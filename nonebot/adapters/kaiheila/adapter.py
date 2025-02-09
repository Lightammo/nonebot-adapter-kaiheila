import asyncio
import inspect
import json
import re
import zlib
from typing import Any, Dict, List, Type, Union, Callable, Optional, Mapping

from nonebot.adapters import Adapter as BaseAdapter
from nonebot.drivers import (
    URL,
    Driver,
    Request,
    WebSocket,
    ForwardDriver,
)
from nonebot.internal.driver import Response
from nonebot.typing import overrides
from nonebot.utils import escape_tag
from pydantic import parse_obj_as
from pygtrie import StringTrie

from . import event
from .api.handle import get_api_method, get_api_restype
from .bot import Bot
from .config import Config as KaiheilaConfig
from .event import *
from .event import OriginEvent
from .exception import ApiNotAvailable, ReconnectError, TokenError, UnauthorizedException, RateLimitException, \
    ActionFailed, KaiheilaAdapterException, NetworkError
from .message import Message, MessageSegment
from .utils import ResultStore, log, _handle_api_result

RECONNECT_INTERVAL = 3.0


class Adapter(BaseAdapter):
    # init all event models
    event_models: StringTrie = StringTrie(separator=".")
    for model_name in dir(event):
        model = getattr(event, model_name)
        if not inspect.isclass(model) or not issubclass(model, OriginEvent):
            continue
        event_models["." + model.__event__] = model

    @overrides(BaseAdapter)
    def __init__(self, driver: Driver, **kwargs: Any):
        super().__init__(driver, **kwargs)
        self.kaiheila_config: KaiheilaConfig = KaiheilaConfig(**self.config.dict())
        self.api_root = f'https://www.kaiheila.cn/api/v3/'
        self.connections: Dict[str, WebSocket] = {}
        self.tasks: List[asyncio.Task] = []
        self.setup()

    # OK
    @classmethod
    @overrides(BaseAdapter)
    def get_name(cls) -> str:
        return "Kaiheila"

    # OK
    def setup(self) -> None:
        if not isinstance(self.driver, ForwardDriver):
            log(
                "WARNING",
                f"Current driver {self.config.driver} don't support forward connections! Ignored",
            )
        else:
            self.driver.on_startup(self.start_forward)
            self.driver.on_shutdown(self.stop_forward)
            self.driver.on_bot_connect(self.start_heartbeat)

    @overrides(BaseAdapter)
    async def request(self, setup: Request) -> Response:
        try:
            response = await super().request(setup)
            if 200 <= response.status_code < 300:
                if not response.content:
                    raise ValueError("Empty response")
                return response
            elif response.status_code == 403:
                raise UnauthorizedException(response)
            elif response.status_code in (404, 405):
                raise ApiNotAvailable
            elif response.status_code == 429:
                raise RateLimitException(response)
            else:
                raise ActionFailed(response)
        except KaiheilaAdapterException:
            raise
        except Exception as e:
            raise NetworkError("API request failed") from e

    @overrides(BaseAdapter)
    async def _call_api(self, bot: Bot, api: str, **data) -> Any:
        if isinstance(self.driver, ForwardDriver):
            if not self.api_root:
                raise ApiNotAvailable()

            match = re.findall(r'[A-Z]', api)
            if len(match) > 0:
                for m in match:
                    api = api.replace(m, "-" + m.lower())
            api = api.replace("_", "/")

            if api.startswith("/api/v3/"):
                api = api[len("/api/v3/"):]
            elif api.startswith("api/v3"):
                api = api[len("api/v3"):]
            api = api.strip("/")
            return await self._do_call_api(api, data, bot.token)

        else:
            raise ApiNotAvailable

    async def _do_call_api(self, api: str,
                           data: Optional[Mapping[str, Any]] = None,
                           token: Optional[str] = None) -> Any:
        log("DEBUG", f"Calling API <y>{api}</y>")
        data = dict(data) if data is not None else dict()

        # 判断 POST 或 GET
        method = get_api_method(api) if not data.get("method") else data.get("method")

        headers = data.get("headers", {})

        files = None
        query = None
        body = None

        if "files" in data:
            files = data["files"]
            del data["files"]
        elif "file" in data:  # 目前只有asset/create接口需要上传文件（大概）
            files = {"file": data["file"]}
            del data["file"]

        if method == "GET":
            query = data
        elif method == "POST":
            body = data

        if token is not None:
            headers["Authorization"] = f"Bot {token}"

        request = Request(
            method,
            self.api_root + api,
            headers=headers,
            params=query,
            data=body,
            files=files,
            timeout=self.config.api_timeout,
        )
        result_type = get_api_restype(api)
        try:
            resp = await self.request(request)
            result = _handle_api_result(resp)
            return parse_obj_as(result_type, result) if result_type else None
        except Exception as e:
            raise e

    async def _get_bot_info(self, token: str) -> User:
        return await self._do_call_api("user/me", token=token)

    async def _get_gateway(self, token: str) -> URL:
        result = await self._do_call_api("gateway/index",
                                         data={'compress': 1 if self.kaiheila_config.compress else 0},
                                         token=token)
        return result.url

    async def start_forward(self) -> None:
        for bot in self.kaiheila_config.kaiheila_bots:
            bot_token = bot.token
            try:
                url = await self._get_gateway(bot_token)
                ws_url = URL(url)
                self.tasks.append(asyncio.create_task(self._forward_ws(ws_url, bot_token)))
            except TokenError as e:
                log(
                    "ERROR",
                    f"<r><bg #f8bbd0>Error token {bot_token} ,"
                    "please get the new token from https://developer.kaiheila.cn/app/index </bg #f8bbd0></r>",
                    e,
                )
            except Exception as e:
                log(
                    "ERROR",
                    f"<r><bg #f8bbd0>Error {bot_token} "
                    "to get the Gateway</bg #f8bbd0></r>",
                    e,
                )

    async def stop_forward(self) -> None:
        for task in self.tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _forward_ws(self, url: URL, bot_token: str) -> None:
        headers = {}
        if bot_token:
            headers[
                "Authorization"
            ] = f"Bot {bot_token}"
        request = Request("GET", url, headers=headers)

        bot: Optional[Bot] = None

        while True:
            try:
                async with self.websocket(request) as ws:
                    log(
                        "DEBUG",
                        f"WebSocket Connection to {escape_tag(str(url))} established",
                    )
                    try:
                        data_decompress_func = zlib.decompress if self.kaiheila_config.compress else lambda x: x
                        while True:
                            data = await ws.receive()
                            data = data_decompress_func(data)
                            json_data = json.loads(data)
                            event = self.json_to_event(json_data, bot and bot.self_id)
                            if not event:
                                continue
                            if not bot:
                                if (
                                        not isinstance(event, LifecycleMetaEvent)
                                        or event.sub_type != "connect"
                                ):
                                    continue
                                bot_info = await self._get_bot_info(bot_token)
                                self_id = bot_info.id_
                                bot = Bot(self, self_id, bot_info.username, bot_token)
                                self.connections[self_id] = ws
                                self.bot_connect(bot)

                                log(
                                    "INFO",
                                    f"<y>Bot {escape_tag(self_id)}</y> connected",
                                )
                            asyncio.create_task(bot.handle_event(event))
                    except ReconnectError as e:
                        log(
                            "ERROR",
                            "<r><bg #f8bbd0>The current connection has expired"
                            f"for bot {escape_tag(bot.self_id)}. Trying to reconnect...</bg #f8bbd0></r>",
                            e,
                        )
                        break
                    except TokenError:
                        raise
                    except Exception as e:
                        log(
                            "ERROR",
                            "<r><bg #f8bbd0>Error while process data from websocket"
                            f"{escape_tag(str(url))}. Trying to reconnect...</bg #f8bbd0></r>",
                            e,
                        )
                        break
                    finally:
                        try:
                            await ws.close()
                        except Exception:
                            pass
                        if bot:
                            # 重新获取 gateway
                            url = await self._get_gateway(token=bot_token)
                            request = Request("GET", url, headers=headers)
                            # 清空本地的 sn 计数
                            ResultStore.set_sn(bot.self_id, 0)
                            self.connections.pop(bot.self_id, None)
                            self.bot_disconnect(bot)
                            bot = None
            except Exception as e:
                log(
                    "ERROR",
                    "<r><bg #f8bbd0>Error while setup websocket to "
                    f"{escape_tag(str(url))}. Trying to reconnect...</bg #f8bbd0></r>",
                    e,
                )

            await asyncio.sleep(RECONNECT_INTERVAL)

    async def start_heartbeat(self, bot: Bot) -> None:
        """
        每30s一次心跳
        :return:
        """
        while self.connections.get(bot.self_id):
            if self.connections.get(bot.self_id).closed:
                break
            await self.connections.get(bot.self_id).send(json.dumps({
                "s": 2,
                "sn": ResultStore.get_sn(bot.self_id)  # 客户端目前收到的最新的消息 sn
            }))
            await asyncio.sleep(26)

    @classmethod
    def json_to_event(
            cls, json_data: Any, self_id: Optional[str] = None,
    ) -> Optional[Event]:
        if not isinstance(json_data, dict):
            return None

        signal = json_data['s']

        if signal == SignalTypes.HELLO:
            if json_data['d']['code'] == 0:
                data = json_data['d']
                data["post_type"] = "meta_event"
                data["sub_type"] = 'connect'
                data["meta_event_type"] = "lifecycle"
                return LifecycleMetaEvent.parse_obj(data)
            elif json_data['d']['code'] == 40103:
                raise ReconnectError
            elif json_data['d']['code'] == 40101:
                raise TokenError("无效的 token")
            elif json_data['d']['code'] == 40102:
                raise TokenError("token 验证失败")
        elif signal == SignalTypes.PONG:
            data = dict()
            data["post_type"] = "meta_event"
            data["meta_event_type"] = "heartbeat"
            log(
                "DEBUG",
                f"<y>Bot {escape_tag(str(self_id))}</y> HeartBeat",
            )
            return HeartbeatMetaEvent.parse_obj(data)
        elif signal == SignalTypes.EVENT:
            ResultStore.set_sn(self_id, json_data["sn"])
        elif signal == SignalTypes.RECONNECT:
            raise ReconnectError
        elif signal == SignalTypes.RESUME_ACK:
            # 不存在的signal，resume是不可能resume的，这辈子都不会resume的，出了问题直接重连
            return

        # 屏蔽 Bot 自身的消息
        if json_data["d"].get("author_id") == self_id:
            return

        try:
            data = json_data['d']
            extra = data.get("extra")

            data['self_id'] = self_id
            data['group_id'] = data.get('target_id')
            data['time'] = data.get('msg_timestamp')
            data['user_id'] = data.get('author_id') if data.get('author_id') != "1" else "SYSTEM"

            if data['type'] == EventTypes.sys:
                data['post_type'] = "notice"
                data['notice_type'] = extra.get('type')
                message = Message.template("{}").format(data["content"])
                data['message'] = message
                # data['notice_type'] = data.get('channel_type').lower()
                # data['notice_type'] = 'private' if data['notice_type'] == 'person' else data['notice_type']
            else:
                data['post_type'] = "message"
                data['sub_type'] = [i.name.lower() for i in EventTypes if i.value == extra.get('type')][0]
                data['message_type'] = data.get('channel_type').lower()
                data['message_type'] = 'private' if data['message_type'] == 'person' else data['message_type']
                data['extra']['content'] = data.get('content')
                data['event'] = data['extra']

            data['message_id'] = data.get('msg_id')
            post_type = data['post_type']
            detail_type = data.get(f"{post_type}_type")
            detail_type = f".{detail_type}" if detail_type else ""
            sub_type = data.get('sub_type')
            sub_type = f".{sub_type}" if sub_type else ""

            models = cls.get_event_model(post_type + detail_type + sub_type)
            for model in models:
                try:
                    event = model.parse_obj(data)
                    break
                except Exception as e:
                    log("DEBUG", "Event Parser Error", e)
            else:
                event = Event.parse_obj(json_data)
            log("DEBUG", str(event.dict()))
            return event
        except Exception as e:
            log(
                "ERROR",
                "<r><bg #f8bbd0>Failed to parse event. "
                f"Raw: {escape_tag(str(json_data))}</bg #f8bbd0></r>",
                e,
            )

    @classmethod
    def add_custom_model(cls, model: Type[Event]) -> None:
        if not model.__event__:
            raise ValueError("Event model's `__event__` attribute must be set")
        cls.event_models["." + model.__event__] = model

    @classmethod
    def get_event_model(cls, event_name: str) -> List[Type[Event]]:
        """
        :说明:

          根据事件名获取对应 ``Event Model`` 及 ``FallBack Event Model`` 列表, 不包括基类 ``Event``

        :返回:

          - ``List[Type[Event]]``
        """
        return [model.value for model in cls.event_models.prefixes("." + event_name)][
               ::-1
               ]

    @classmethod
    def custom_send(
            cls,
            send_func: Callable[[Bot, Event, Union[str, Message, MessageSegment]], None],
    ):
        setattr(Bot, "send_handler", send_func)
