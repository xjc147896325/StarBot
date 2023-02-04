import asyncio
import time
import typing
from asyncio import AbstractEventLoop
from typing import Optional, Any, Union, List

from loguru import logger
from pydantic import BaseModel, PrivateAttr

from .live import LiveDanmaku, LiveRoom
from .model import PushTarget
from .user import User
from ..exception import LiveException
from ..painter.DynamicPicGenerator import DynamicPicGenerator
from ..utils import config, redis
from ..utils.utils import get_credential, timestamp_format, get_unames_and_faces_by_uids

if typing.TYPE_CHECKING:
    from .sender import Bot


class Up(BaseModel):
    """
    主播类
    """

    uid: int
    """主播 UID"""

    targets: List[PushTarget]
    """主播所需推送的所有好友或群"""

    uname: Optional[str] = None
    """主播昵称，无需手动传入，会自动获取"""

    room_id: Optional[int] = None
    """主播直播间房间号，无需手动传入，会自动获取"""

    __user: Optional[User] = PrivateAttr()
    """用户实例，用于获取用户相关信息"""

    __live_room: Optional[LiveRoom] = PrivateAttr()
    """直播间实例，用于获取直播间相关信息"""

    __room: Optional[LiveDanmaku] = PrivateAttr()
    """直播间连接实例"""

    __is_reconnect: Optional[bool] = PrivateAttr()
    """是否为重新连接直播间"""

    __loop: Optional[AbstractEventLoop] = PrivateAttr()
    """asyncio 事件循环"""

    __bot: Optional["Bot"] = PrivateAttr()
    """主播所关联 Bot 实例"""

    def __init__(self, **data: Any):
        super().__init__(**data)
        self.__user = None
        self.__live_room = None
        self.__room = None
        self.__is_reconnect = False
        self.__loop = asyncio.get_event_loop()
        self.__bot = None

    def inject_bot(self, bot):
        self.__bot = bot

    def dispatch(self, name, data):
        self.__room.dispatch(name, data)

    async def accumulate_and_reset_data(self):
        await redis.accumulate_data(self.room_id)
        await redis.reset_data(self.room_id)

    def is_connecting(self):
        return (self.__room is not None) and (self.__room.get_status() != 2)

    def __any_live_on_enabled(self):
        return any(map(lambda conf: conf.enabled, map(lambda group: group.live_on, self.targets)))

    def __any_live_off_enabled(self):
        return any(map(lambda conf: conf.enabled, map(lambda group: group.live_off, self.targets)))

    def __any_live_report_enabled(self):
        return any(map(lambda conf: conf.enabled, map(lambda group: group.live_report, self.targets)))

    def __any_live_report_item_enabled(self, attribute: Union[str, List[str]]):
        if isinstance(attribute, list):
            return any([self.__any_live_report_item_enabled(a) for a in attribute])
        return any(map(lambda t: t.live_report.enabled and t.live_report.__getattribute__(attribute), self.targets))

    def __any_dynamic_update_enabled(self):
        return any(map(lambda conf: conf.enabled, map(lambda group: group.dynamic_update, self.targets)))

    async def connect(self):
        """
        连接直播间
        """
        self.__user = User(self.uid, get_credential())

        if not all([self.uname, self.room_id]):
            user_info = await self.__user.get_user_info()
            self.uname = user_info["name"]
            if user_info["live_room"] is None:
                raise LiveException(f"UP 主 {self.uname} ( UID: {self.uid} ) 还未开通直播间")
            self.room_id = user_info["live_room"]["roomid"]

        # 开播推送开关和下播推送开关均处于关闭状态时跳过连接直播间，以节省性能
        if config.get("ONLY_CONNECT_NECESSARY_ROOM"):
            if not any([self.__any_live_on_enabled(), self.__any_live_off_enabled(),
                        self.__any_live_report_enabled()]):
                logger.warning(f"{self.uname} 的开播, 下播和直播报告开关均处于关闭状态, 跳过连接直播间")
                return

        self.__live_room = LiveRoom(self.room_id, get_credential())
        self.__room = LiveDanmaku(self.room_id, credential=get_credential())

        logger.opt(colors=True).info(f"准备连接到 <cyan>{self.uname}</> 的直播间 <cyan>{self.room_id}</>")

        self.__loop.create_task(self.__room.connect())

        @self.__room.on("VERIFICATION_SUCCESSFUL")
        async def on_link(event):
            """
            连接成功事件
            """
            logger.debug(f"{self.uname} (VERIFICATION_SUCCESSFUL): {event}")

            if self.__is_reconnect:
                logger.success(f"已重新连接到 {self.uname} 的直播间 {self.room_id}")

                room_info = await self.__live_room.get_room_play_info()
                last_status = await redis.get_live_status(self.room_id)
                now_status = room_info["live_status"]

                if now_status != last_status:
                    await redis.set_live_status(self.room_id, now_status)
                    if now_status == 1:
                        logger.warning(f"直播间 {self.room_id} 断线期间开播")
                        param = {
                            "data": {
                                "live_time": 0
                            }
                        }
                        await live_on(param)
                    if last_status == 1:
                        logger.warning(f"直播间 {self.room_id} 断线期间下播")
                        param = {}
                        await live_off(param)
            else:
                logger.success(f"已成功连接到 {self.uname} 的直播间 {self.room_id}")

                self.__is_reconnect = True

        @self.__room.on("LIVE")
        async def live_on(event):
            """
            开播事件
            """
            logger.debug(f"{self.uname} (LIVE): {event}")

            # 是否为真正开播
            if "live_time" in event["data"]:
                room_info = await self.__live_room.get_room_info()
                self.uname = room_info["anchor_info"]["base_info"]["uname"]

                await redis.set_live_status(self.room_id, 1)

                # 是否为主播网络波动断线重连
                now = int(time.time())
                last = await redis.get_live_end_time(self.room_id)
                is_reconnect = (now - last) <= config.get("UP_DISCONNECT_CONNECT_INTERVAL")
                if is_reconnect:
                    logger.opt(colors=True).info(f"<magenta>[断线重连] {self.uname} ({self.room_id})</>")
                    if config.get("UP_DISCONNECT_CONNECT_MESSAGE"):
                        self.__bot.send_to_all_target(self, config.get("UP_DISCONNECT_CONNECT_MESSAGE"),
                                                      lambda t: t.live_on.enabled)
                else:
                    logger.opt(colors=True).info(f"<magenta>[开播] {self.uname} ({self.room_id})</>")

                    live_start_time = room_info["room_info"]["live_start_time"]
                    fans_count = room_info["anchor_info"]["relation_info"]["attention"]
                    if room_info["anchor_info"]["medal_info"] is None:
                        fans_medal_count = 0
                    else:
                        fans_medal_count = room_info["anchor_info"]["medal_info"]["fansclub"]
                    guard_count = room_info["guard_info"]["count"]
                    await redis.set_live_start_time(self.room_id, live_start_time)
                    await redis.set_fans_count(self.room_id, live_start_time, fans_count)
                    await redis.set_fans_medal_count(self.room_id, live_start_time, fans_medal_count)
                    await redis.set_guard_count(self.room_id, live_start_time, guard_count)

                    await self.accumulate_and_reset_data()

                    # 推送开播消息
                    arg_base = room_info["room_info"]
                    args = {
                        "{uname}": self.uname,
                        "{title}": arg_base["title"],
                        "{url}": f"https://live.bilibili.com/{self.room_id}",
                        "{cover}": "".join(["{urlpic=", arg_base["cover"], "}"])
                    }
                    await self.__bot.send_live_on_at(self)
                    self.__bot.send_live_on(self, args)

        @self.__room.on("PREPARING")
        async def live_off(event):
            """
            下播事件
            """
            logger.debug(f"{self.uname} (PREPARING): {event}")

            await redis.set_live_status(self.room_id, 0)
            await redis.set_live_end_time(self.room_id, int(time.time()))

            logger.opt(colors=True).info(f"<magenta>[下播] {self.uname} ({self.room_id})</>")

            # 生成下播消息和直播报告占位符参数
            live_off_args = {
                "{uname}": self.uname
            }
            live_report_param = await self.__generate_live_report_param()

            # 推送下播消息和直播报告
            self.__bot.send_live_off(self, live_off_args)
            self.__bot.send_live_report(self, live_report_param)

        danmu_items = ["danmu", "danmu_ranking", "danmu_diagram", "danmu_cloud"]
        if not config.get("ONLY_HANDLE_NECESSARY_EVENT") or self.__any_live_report_item_enabled(danmu_items):
            @self.__room.on("DANMU_MSG")
            async def on_danmu(event):
                """
                弹幕事件
                """
                logger.debug(f"{self.uname} (DANMU_MSG): {event}")

                base = event["data"]["info"]
                uid = base[2][0]
                content = base[1]

                # 弹幕统计
                await redis.incr_room_danmu_count(self.room_id)
                await redis.incr_user_danmu_count(self.room_id, uid)

                # 弹幕词云所需弹幕记录
                if isinstance(base[0][13], str):
                    await redis.add_room_danmu(self.room_id, content)
                    await redis.incr_room_danmu_time(self.room_id, int(time.time()))

        gift_items = [
            "box", "gift", "box_ranking", "box_profit_ranking", "gift_ranking",
            "box_profit_diagram", "box_diagram", "gift_diagram"
        ]
        if not config.get("ONLY_HANDLE_NECESSARY_EVENT") or self.__any_live_report_item_enabled(gift_items):
            @self.__room.on("SEND_GIFT")
            async def on_gift(event):
                """
                礼物事件
                """
                logger.debug(f"{self.uname} (SEND_GIFT): {event}")

                base = event["data"]["data"]
                uid = base["uid"]
                num = base["num"]
                price = float("{:.1f}".format((base["discount_price"] / 1000) * num))

                # 礼物统计
                if base["total_coin"] != 0 and base["discount_price"] != 0:
                    await redis.incr_room_gift_profit(self.room_id, price)
                    await redis.incr_user_gift_profit(self.room_id, uid, price)

                    await redis.incr_room_gift_time(self.room_id, int(time.time()), price)

                # 盲盒统计
                if base["blind_gift"] is not None:
                    box_price = base["total_coin"] / 1000
                    gift_num = base["num"]
                    gift_price = base["discount_price"] / 1000
                    profit = float("{:.1f}".format((gift_price * gift_num) - box_price))

                    await redis.incr_room_box_count(self.room_id, gift_num)
                    await redis.incr_user_box_count(self.room_id, uid, gift_num)
                    box_profit_after = await redis.incr_room_box_profit(self.room_id, profit)
                    await redis.incr_user_box_profit(self.room_id, uid, profit)

                    await redis.add_room_box_profit_record(self.room_id, box_profit_after)
                    await redis.incr_room_box_time(self.room_id, int(time.time()))

        sc_items = ["sc", "sc_ranking", "sc_diagram"]
        if not config.get("ONLY_HANDLE_NECESSARY_EVENT") or self.__any_live_report_item_enabled(sc_items):
            @self.__room.on("SUPER_CHAT_MESSAGE")
            async def on_sc(event):
                """
                SC（醒目留言）事件
                """
                logger.debug(f"{self.uname} (SUPER_CHAT_MESSAGE): {event}")

                base = event["data"]["data"]
                uid = base["uid"]
                price = base["price"]

                # SC 统计
                await redis.incr_room_sc_profit(self.room_id, price)
                await redis.incr_user_sc_profit(self.room_id, uid, price)

                await redis.incr_room_sc_time(self.room_id, int(time.time()), price)

        guard_items = ["guard", "guard_list", "guard_diagram"]
        if not config.get("ONLY_HANDLE_NECESSARY_EVENT") or self.__any_live_report_item_enabled(guard_items):
            @self.__room.on("GUARD_BUY")
            async def on_guard(event):
                """
                大航海事件
                """
                logger.debug(f"{self.uname} (GUARD_BUY): {event}")

                base = event["data"]["data"]
                uid = base["uid"]
                guard_type = base["gift_name"]
                month = base["num"]

                # 上舰统计
                type_mapping = {
                    "舰长": "Captain",
                    "提督": "Commander",
                    "总督": "Governor"
                }
                await redis.incr_room_guard_count(type_mapping[guard_type], self.room_id, month)
                await redis.incr_user_guard_count(type_mapping[guard_type], self.room_id, uid, month)

                await redis.incr_room_guard_time(self.room_id, int(time.time()), month)

        if self.__any_dynamic_update_enabled():
            @self.__room.on("DYNAMIC_UPDATE")
            async def dynamic_update(event):
                """
                动态更新事件
                """
                logger.debug(f"{self.uname} (DYNAMIC_UPDATE): {event}")

                dynamic_id = event["desc"]["dynamic_id"]
                dynamic_type = event["desc"]["type"]
                bvid = event['desc']['bvid'] if dynamic_type == 8 else ""
                rid = event['desc']['rid'] if dynamic_type in (64, 256) else ""

                action_map = {
                    1: "转发了动态",
                    2: "发表了新动态",
                    4: "发表了新动态",
                    8: "投稿了新视频",
                    64: "投稿了新专栏",
                    256: "投稿了新音频",
                    2048: "发表了新动态"
                }
                url_map = {
                    1: f"https://t.bilibili.com/{dynamic_id}",
                    2: f"https://t.bilibili.com/{dynamic_id}",
                    4: f"https://t.bilibili.com/{dynamic_id}",
                    8: f"https://www.bilibili.com/video/{bvid}",
                    64: f"https://www.bilibili.com/read/cv{rid}",
                    256: f"https://www.bilibili.com/audio/au{rid}",
                    2048: f"https://t.bilibili.com/{dynamic_id}"
                }
                base64str = await DynamicPicGenerator.generate(event)

                # 推送动态消息
                dynamic_update_args = {
                    "{uname}": self.uname,
                    "{action}": action_map.get(dynamic_type, "发表了新动态"),
                    "{url}": url_map.get(dynamic_type, f"https://t.bilibili.com/{dynamic_id}"),
                    "{picture}": "".join(["{base64pic=", base64str, "}"])
                }
                await self.__bot.send_dynamic_at(self)
                self.__bot.send_dynamic_update(self, dynamic_update_args)

    async def __generate_live_report_param(self):
        """
        计算直播报告所需数据
        """
        live_report_param = {}

        # 主播信息
        live_report_param.update({
            "uname": self.uname,
            "room_id": self.room_id
        })

        # 直播时间段和直播时长
        start_time = await redis.get_live_start_time(self.room_id)
        end_time = await redis.get_live_end_time(self.room_id)
        seconds = end_time - start_time
        minute, second = divmod(seconds, 60)
        hour, minute = divmod(minute, 60)

        live_report_param.update({
            "start_timestamp": start_time,
            "end_timestamp": end_time,
            "start_time": timestamp_format(start_time, "%m/%d %H:%M:%S"),
            "end_time": timestamp_format(end_time, "%m/%d %H:%M:%S"),
            "hour": hour,
            "minute": minute,
            "second": second
        })

        # 基础数据变动
        if self.__any_live_report_item_enabled(["fans_change", "fans_medal_change", "guard_change"]):
            room_info = await self.__live_room.get_room_info()

            if await redis.exists_fans_count(self.room_id, start_time):
                fans_count = await redis.get_fans_count(self.room_id, start_time)
            else:
                fans_count = -1
            if await redis.exists_fans_medal_count(self.room_id, start_time):
                fans_medal_count = await redis.get_fans_medal_count(self.room_id, start_time)
            else:
                fans_medal_count = -1
            if await redis.exists_guard_count(self.room_id, start_time):
                guard_count = await redis.get_guard_count(self.room_id, start_time)
            else:
                guard_count = -1

            if room_info["anchor_info"]["medal_info"] is None:
                fans_medal_count_after = 0
            else:
                fans_medal_count_after = room_info["anchor_info"]["medal_info"]["fansclub"]

            live_report_param.update({
                # 粉丝变动
                "fans_before": fans_count,
                "fans_after": room_info["anchor_info"]["relation_info"]["attention"],
                # 粉丝团（粉丝勋章数）变动
                "fans_medal_before": fans_medal_count,
                "fans_medal_after": fans_medal_count_after,
                # 大航海变动
                "guard_before": guard_count,
                "guard_after": room_info["guard_info"]["count"]
            })

        # 直播数据
        box_profit = await redis.get_room_box_profit(self.room_id)
        count = await redis.len_box_profit_record()
        await redis.add_box_profit_record(start_time, self.uid, self.uname, box_profit)
        rank = await redis.rank_box_profit_record(start_time, self.uid, self.uname)
        percent = float("{:.2f}".format(float("{:.4f}".format(rank / count)) * 100)) if count != 0 else 100

        live_report_param.update({
            # 弹幕相关
            "danmu_count": await redis.get_room_danmu_count(self.room_id),
            "danmu_person_count": await redis.len_user_danmu_count(self.room_id),
            "danmu_diagram": await redis.get_room_danmu_time(self.room_id),
            # 盲盒相关
            "box_count": await redis.get_room_box_count(self.room_id),
            "box_person_count": await redis.len_user_box_count(self.room_id),
            "box_profit": box_profit,
            "box_beat_percent": percent,
            "box_profit_diagram": await redis.get_room_box_profit_record(self.room_id),
            "box_diagram": await redis.get_room_box_time(self.room_id),
            # 礼物相关
            "gift_profit": await redis.get_room_gift_profit(self.room_id),
            "gift_person_count": await redis.len_user_gift_profit(self.room_id),
            "gift_diagram": await redis.get_room_gift_time(self.room_id),
            # SC（醒目留言）相关
            "sc_profit": await redis.get_room_sc_profit(self.room_id),
            "sc_person_count": await redis.len_user_sc_profit(self.room_id),
            "sc_diagram": await redis.get_room_sc_time(self.room_id),
            # 大航海相关
            "captain_count": await redis.get_room_captain_count(self.room_id),
            "commander_count": await redis.get_room_commander_count(self.room_id),
            "governor_count": await redis.get_room_governor_count(self.room_id),
            "guard_diagram": await redis.get_room_guard_time(self.room_id)
        })

        # 弹幕排行
        if self.__any_live_report_item_enabled("danmu_ranking"):
            ranking_count = max(map(lambda t: t.live_report.danmu_ranking, self.targets))
            danmu_ranking = await redis.rev_range_user_danmu_count(self.room_id, 0, ranking_count - 1)

            if danmu_ranking:
                uids = [x[0] for x in danmu_ranking]
                counts = [x[1] for x in danmu_ranking]
                unames, faces = await get_unames_and_faces_by_uids(uids)

                live_report_param.update({
                    "danmu_ranking_faces": faces,
                    "danmu_ranking_unames": unames,
                    "danmu_ranking_counts": counts
                })

        # 盲盒数量排行
        if self.__any_live_report_item_enabled("box_ranking"):
            ranking_count = max(map(lambda t: t.live_report.box_ranking, self.targets))
            box_ranking = await redis.rev_range_user_box_count(self.room_id, 0, ranking_count - 1)

            if box_ranking:
                uids = [x[0] for x in box_ranking]
                counts = [x[1] for x in box_ranking]
                unames, faces = await get_unames_and_faces_by_uids(uids)

                live_report_param.update({
                    "box_ranking_faces": faces,
                    "box_ranking_unames": unames,
                    "box_ranking_counts": counts
                })

        # 盲盒盈亏排行
        if self.__any_live_report_item_enabled("box_profit_ranking"):
            ranking_count = max(map(lambda t: t.live_report.box_profit_ranking, self.targets))
            box_profit_ranking = await redis.rev_range_user_box_profit(self.room_id, 0, ranking_count - 1)

            if box_profit_ranking:
                uids = [x[0] for x in box_profit_ranking]
                counts = [x[1] for x in box_profit_ranking]
                unames, faces = await get_unames_and_faces_by_uids(uids)

                live_report_param.update({
                    "box_profit_ranking_faces": faces,
                    "box_profit_ranking_unames": unames,
                    "box_profit_ranking_counts": counts
                })

        # 礼物排行
        if self.__any_live_report_item_enabled("gift_ranking"):
            ranking_count = max(map(lambda t: t.live_report.gift_ranking, self.targets))
            gift_ranking = await redis.rev_range_user_gift_profit(self.room_id, 0, ranking_count - 1)

            if gift_ranking:
                uids = [x[0] for x in gift_ranking]
                counts = [x[1] for x in gift_ranking]
                unames, faces = await get_unames_and_faces_by_uids(uids)

                live_report_param.update({
                    "gift_ranking_faces": faces,
                    "gift_ranking_unames": unames,
                    "gift_ranking_counts": counts
                })

        # SC（醒目留言）排行
        if self.__any_live_report_item_enabled("sc_ranking"):
            ranking_count = max(map(lambda t: t.live_report.sc_ranking, self.targets))
            sc_ranking = await redis.rev_range_user_sc_profit(self.room_id, 0, ranking_count - 1)

            if sc_ranking:
                uids = [x[0] for x in sc_ranking]
                counts = [x[1] for x in sc_ranking]
                unames, faces = await get_unames_and_faces_by_uids(uids)

                live_report_param.update({
                    "sc_ranking_faces": faces,
                    "sc_ranking_unames": unames,
                    "sc_ranking_counts": counts
                })

        # 开通大航海观众列表
        if self.__any_live_report_item_enabled("guard_list"):
            captains = await redis.rev_range_user_captain_count(self.room_id)
            commanders = await redis.rev_range_user_commander_count(self.room_id)
            governors = await redis.rev_range_user_governor_count(self.room_id)

            if captains:
                uids = [x[0] for x in captains]
                counts = [x[1] for x in captains]
                unames, faces = await get_unames_and_faces_by_uids(uids)

                captain_infos = [[faces[i], unames[i], counts[i]] for i in range(len(counts))]
                live_report_param.update({
                    "captain_infos": captain_infos,
                })

            if commanders:
                uids = [x[0] for x in commanders]
                counts = [x[1] for x in commanders]
                unames, faces = await get_unames_and_faces_by_uids(uids)

                commander_infos = [[faces[i], unames[i], counts[i]] for i in range(len(counts))]
                live_report_param.update({
                    "commander_infos": commander_infos,
                })

            if governors:
                uids = [x[0] for x in governors]
                counts = [x[1] for x in governors]
                unames, faces = await get_unames_and_faces_by_uids(uids)

                governor_infos = [[faces[i], unames[i], counts[i]] for i in range(len(counts))]
                live_report_param.update({
                    "governor_infos": governor_infos,
                })

        # 弹幕词云
        if self.__any_live_report_item_enabled("danmu_cloud"):
            all_danmu = await redis.get_room_danmu(self.room_id)

            live_report_param.update({
                "all_danmu": all_danmu
            })

        return live_report_param

    def __eq__(self, other):
        if isinstance(other, Up):
            return self.uid == other.uid
        elif isinstance(other, int):
            return self.uid == other
        return False

    def __hash__(self):
        return hash(self.uid)
