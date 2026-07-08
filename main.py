import json
import os
import sys
import asyncio
import time
import httpx
from datetime import datetime, timedelta, timezone

# 确保插件目录在 sys.path 中，使本地模块可被导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.message_components import Plain
from astrbot.api import logger

import web_server
from storage import load_config, add_history_entry

BEIJING_TZ = timezone(timedelta(hours=8))
HTTP_BASE = os.environ.get("ONEBOT_HTTP_BASE", "http://127.0.0.1:3000")
WEB_PORT = int(os.environ.get("WEB_PORT", "1655"))

_last_exec_month = None

# 删除确认状态: {(group_id, user_id): (expiry_timestamp, entries)}
_pending_confirmations = {}
_CONFIRMATION_TIMEOUT = 60

def get_now_beijing():
    return datetime.now(BEIJING_TZ)

def extract_file_list(data: dict) -> list:
    """从可能的字段中提取文件列表（只取根目录文件，不含文件夹）"""
    candidates = [
        data.get("files"),
        data.get("items"),
        data.get("file_list"),
        data.get("data", {}).get("files"),
        data.get("data", {}).get("items"),
        data.get("data", {}).get("file_list"),
    ]
    for cand in candidates:
        if isinstance(cand, list):
            return [f for f in cand if not f.get("is_folder") and f.get("busid") != 2]
    return []


def _format_file_size(size_bytes: int) -> str:
    """将字节数转换为可读大小字符串"""
    if not size_bytes or size_bytes <= 0:
        return "未知大小"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    if idx == 0:
        return f"{int(size)}{units[idx]}"
    return f"{size:.1f}{units[idx]}"


def _build_forward_nodes(
    entries: list,
    group_id: int,
    bot_self_id: str = "1000000",
) -> list:
    """将删除条目列表转换为 OneBot v11 合并转发消息节点"""
    now_str = get_now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    deleted = [e for e in entries if e["status"] == "deleted"]
    failed = [e for e in entries if e["status"] == "failed"]
    skipped = [e for e in entries if e["status"] == "skipped"]

    nodes = []

    # --- 头部节点：概览 ---
    header_text = (
        f"群文件批量删除报告\n"
        f"群号: {group_id}\n"
        f"执行时间: {now_str}\n"
        f"待处理: {len(entries)} 个文件"
    )
    nodes.append({
        "type": "node",
        "data": {
            "nickname": "文件清理助手",
            "user_id": bot_self_id,
            "content": json.dumps(
                [{"type": "text", "data": {"text": header_text}}],
                ensure_ascii=False,
            ),
        },
    })

    # --- 成功删除节点（每组最多10条） ---
    for i in range(0, len(deleted), 10):
        batch = deleted[i:i + 10]
        lines = [f"{i + 1 + j}. {e['file_name']}  [{_format_file_size(e.get('file_size', 0))}]"
                 for j, e in enumerate(batch)]
        nodes.append({
            "type": "node",
            "data": {
                "nickname": "文件清理助手",
                "user_id": bot_self_id,
                "content": json.dumps(
                    [{"type": "text", "data": {"text": "已删除:\n" + "\n".join(lines)}}],
                    ensure_ascii=False,
                ),
            },
        })

    # --- 失败节点 ---
    if failed:
        lines = [f"- {e['file_name']}: {e.get('error', '未知错误')}" for e in failed]
        nodes.append({
            "type": "node",
            "data": {
                "nickname": "文件清理助手",
                "user_id": bot_self_id,
                "content": json.dumps(
                    [{"type": "text", "data": {"text": "删除失败:\n" + "\n".join(lines)}}],
                    ensure_ascii=False,
                ),
            },
        })

    # --- 尾部节点：汇总统计 ---
    footer_parts = [f"成功: {len(deleted)}", f"失败: {len(failed)}"]
    if skipped:
        footer_parts.append(f"跳过: {len(skipped)}")
    footer_parts.append(f"总计: {len(entries)}")
    footer_text = "删除完成\n" + " | ".join(footer_parts)
    nodes.append({
        "type": "node",
        "data": {
            "nickname": "文件清理助手",
            "user_id": bot_self_id,
            "content": json.dumps(
                [{"type": "text", "data": {"text": footer_text}}],
                ensure_ascii=False,
            ),
        },
    })

    return nodes

@register("auto_delete_files", "You", "群文件自动清理插件，支持定时清理、手动命令与自然语言触发", "v6.3.0")
class AutoDeleteFiles(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._task = None
        self._bot = None

    async def initialize(self):
        """插件加载后自动启动后台定时检查任务和 Web 管理面板"""
        self._ensure_task()
        await web_server.start_web_server(HTTP_BASE, WEB_PORT)

    async def terminate(self):
        """插件卸载时取消后台任务并停止 Web 服务"""
        if self._task:
            self._task.cancel()
            self._task = None
        await web_server.stop_web_server()

    def _ensure_task(self):
        if self._task is None:
            async def checker():
                global _last_exec_month
                while True:
                    now = get_now_beijing()
                    cfg = load_config()
                    target_day = cfg.get("auto_clean_day", 1)
                    target_hour, target_minute = 0, 0
                    time_str = cfg.get("auto_clean_time", "00:00")
                    try:
                        parts = time_str.split(":")
                        target_hour = int(parts[0])
                        target_minute = int(parts[1])
                    except (ValueError, IndexError):
                        pass

                    if now.day == target_day and now.hour == target_hour and now.minute == target_minute:
                        month_key = f"{now.year}-{now.month}"
                        if _last_exec_month != month_key:
                            _last_exec_month = month_key
                            logger.info(f"[自动删除] 触发每月{target_day}号{time_str}清理")
                            try:
                                async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=10) as client:
                                    resp = await client.post("/get_group_list")
                                    groups = resp.json().get("data", [])

                                # 并发删除所有群的文件
                                async def clean_one_group(g):
                                    gid = g.get("group_id")
                                    if not gid:
                                        return
                                    entries, logs = await self._delete_all_files(gid)
                                    if entries:
                                        # 记录历史
                                        deleted_count = sum(1 for e in entries if e["status"] == "deleted")
                                        failed_count = sum(1 for e in entries if e["status"] == "failed")
                                        file_names = [e["file_name"] for e in entries if e["status"] == "deleted"]
                                        add_history_entry(int(gid), "自动清理", deleted_count, failed_count, file_names)

                                        # 发送合并转发报告
                                        nodes = _build_forward_nodes(entries, int(gid))
                                        try:
                                            await self._send_group_forward_msg(int(gid), nodes)
                                        except Exception as e:
                                            logger.error(f"[自动删除] 群{gid} 发送报告失败: {e}")

                                        logger.info(
                                            f"[自动删除] 群{gid}: "
                                            f"处理{len(entries)}个文件, 成功{deleted_count}, 失败{failed_count}"
                                        )
                                    else:
                                        logger.info(f"[自动删除] 群{gid}: {logs[0] if logs else '无操作'}")

                                await asyncio.gather(*[clean_one_group(g) for g in groups])
                            except Exception as e:
                                logger.error(f"[自动删除] 获取群列表失败: {e}")
                    await asyncio.sleep(60)
            self._task = asyncio.create_task(checker())

    @staticmethod
    async def _fetch_file_entries(group_id: int) -> tuple:
        """获取群根目录文件列表并构建条目结构。
        返回 (entries, error_msg) 元组；error_msg 非空表示失败。
        """
        try:
            async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=10) as client:
                resp = await client.post("/get_group_root_files", json={"group_id": group_id})
                resp.raise_for_status()
                data = resp.json()
                if data.get("status") != "ok":
                    return [], f"❌ 获取文件列表失败：{data.get('msg', '未知错误')}"
                files = extract_file_list(data)
                if not files:
                    return [], "ℹ️ 根目录没有可删除的文件（请检查 /测文件 确认API返回结构）"

                entries = [
                    {
                        "file_name": f.get("file_name") or f.get("file_id"),
                        "file_id": f.get("file_id"),
                        "file_size": f.get("file_size", 0),
                        "busid": f.get("busid", 0),
                        "upload_time": f.get("upload_time", 0),
                        "uploader": f.get("uploader_name") or f.get("uploader", ""),
                        "status": "pending",
                        "error": None,
                    }
                    for f in files
                ]
                return entries, ""
        except httpx.HTTPStatusError as e:
            return [], f"❌ HTTP 状态异常：{e.response.status_code}"
        except (httpx.RequestError, json.JSONDecodeError) as e:
            return [], f"❌ HTTP 请求异常：{type(e).__name__}: {e}"

    @staticmethod
    async def _delete_all_files(group_id: int, dry_run: bool = False) -> tuple:
        """返回 (entries, logs) 元组。
        entries: 结构化删除条目列表，每项含 file_name/file_id/status/file_size/busid 等字段
        logs:    操作日志列表（每条字符串）
        """
        entries, error_msg = await AutoDeleteFiles._fetch_file_entries(group_id)
        if error_msg:
            return [], [error_msg]

        if dry_run:
            return entries, [f"📋 预览：共 {len(entries)} 个文件待删除"]

        # 执行实际删除
        logs = []
        async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=10) as client:
            for entry in entries:
                fid = entry["file_id"]
                fname = entry["file_name"]

                if not fid:
                    entry["status"] = "skipped"
                    entry["error"] = "缺少 file_id"
                    logs.append(f"⚠️ 跳过（无file_id）：{fname}")
                    continue

                try:
                    del_resp = await client.post("/delete_group_file", json={
                        "group_id": group_id,
                        "file_id": fid,
                    })
                    del_data = del_resp.json()
                except Exception as e:
                    entry["status"] = "failed"
                    entry["error"] = f"{type(e).__name__}: {e}"
                    logs.append(f"❌ 删失败：{fname} -> {entry['error']}")
                    continue

                if del_data.get("status") == "ok":
                    entry["status"] = "deleted"
                    logs.append(f"🗑️ 已删：{fname}")
                else:
                    entry["status"] = "failed"
                    entry["error"] = del_data.get("msg", "未知")
                    logs.append(f"❌ 删失败：{fname} -> {entry['error']}")

        return entries, logs if logs else ["ℹ️ 无操作"]

    def _get_group_id(self, event):
        gid = getattr(event, "group_id", None)
        if gid:
            return gid
        obj = getattr(event, "message_obj", None)
        if obj:
            gid = getattr(obj, "group_id", None)
            if gid:
                return gid
            if isinstance(obj, dict):
                return obj.get("group_id")
        return None

    def _get_sender_id(self, event):
        sid = getattr(event, "user_id", None)
        if sid:
            return str(sid)
        obj = getattr(event, "message_obj", None)
        if obj:
            sid = getattr(obj, "user_id", None)
            if sid:
                return str(sid)
            if isinstance(obj, dict):
                return str(obj.get("user_id", ""))
        return ""

    async def _check_is_admin(self, group_id: int, user_id: str) -> bool:
        """通过 OneBot API 校验用户是否为群主/管理员"""
        try:
            async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=8) as client:
                resp = await client.post(
                    "/get_group_member_info",
                    json={"group_id": group_id, "user_id": int(user_id)},
                )
                data = resp.json()
                role = data.get("data", {}).get("role", "member")
                return role in ("owner", "admin")
        except (httpx.RequestError, json.JSONDecodeError, ValueError) as e:
            logger.error(f"[权限检查] 失败: {e}")
            return False

    @staticmethod
    async def _send_group_forward_msg(group_id: int, nodes: list) -> dict:
        """调用 OneBot HTTP API 发送群合并转发消息"""
        async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=15) as client:
            resp = await client.post("/send_group_forward_msg", json={
                "group_id": group_id,
                "messages": nodes,
            })
            resp.raise_for_status()
            return resp.json()

    @filter.command("立即删除")
    async def delete_now(self, event: AstrMessageEvent):
        self._ensure_task()
        self._bot = event.bot
        gid = self._get_group_id(event)
        if not gid:
            yield event.chain_result([Plain("❌ 请在群聊中使用")])
            return

        sender_id = self._get_sender_id(event)

        # 权限校验
        if not await self._check_is_admin(int(gid), sender_id):
            yield event.chain_result([Plain("❌ 权限不足：仅群主和管理员可以执行此操作")])
            return

        # 获取待删除文件预览
        entries, logs = await self._delete_all_files(int(gid), dry_run=True)
        if not entries:
            yield event.chain_result([Plain(logs[0])])
            return

        # 构建预览消息
        now = time.time()
        _pending_confirmations[(int(gid), sender_id)] = (now + _CONFIRMATION_TIMEOUT, int(gid))

        total_size = sum(e.get("file_size", 0) for e in entries)
        preview_lines = [
            f"⚠️ 即将删除群 {gid} 根目录下 {len(entries)} 个文件（共 {_format_file_size(total_size)}）："
        ]
        for i, e in enumerate(entries[:20]):
            preview_lines.append(
                f"  {i+1}. {e['file_name']}  [{_format_file_size(e.get('file_size', 0))}]"
            )
        if len(entries) > 20:
            preview_lines.append(f"  ... 还有 {len(entries) - 20} 个文件")
        preview_lines.append("")
        preview_lines.append(f'请在 {_CONFIRMATION_TIMEOUT} 秒内回复 "确认删除" 以执行操作')

        yield event.chain_result([Plain("\n".join(preview_lines))])

    @filter.command("确认删除")
    async def confirm_delete(self, event: AstrMessageEvent):
        gid = self._get_group_id(event)
        sender_id = self._get_sender_id(event)
        if not gid:
            yield event.chain_result([Plain("❌ 请在群聊中使用")])
            return

        key = (int(gid), sender_id)
        pending = _pending_confirmations.get(key)
        if not pending:
            yield event.chain_result([Plain("ℹ️ 没有待确认的删除操作，请先使用 /立即删除")])
            return

        expiry, _group_id = pending
        if time.time() > expiry:
            del _pending_confirmations[key]
            yield event.chain_result([Plain("⏰ 确认已超时，请重新使用 /立即删除")])
            return

        del _pending_confirmations[key]
        yield event.chain_result([Plain("⏳ 正在执行删除...")])

        entries, logs = await self._delete_all_files(int(gid))
        if not entries:
            yield event.chain_result([Plain(logs[0])])
            return

        deleted_count = sum(1 for e in entries if e["status"] == "deleted")
        failed_count = sum(1 for e in entries if e["status"] == "failed")

        # 记录历史
        sender_name = getattr(event, "sender_name", None) or sender_id
        file_names = [e["file_name"] for e in entries if e["status"] == "deleted"]
        add_history_entry(int(gid), sender_name, deleted_count, failed_count, file_names)

        nodes = _build_forward_nodes(entries, int(gid))
        try:
            await self._send_group_forward_msg(int(gid), nodes)
        except Exception as e:
            logger.error(f"[确认删除] 发送合并转发失败: {e}")

        summary = f"删除完成：成功 {deleted_count} 个"
        if failed_count:
            summary += f"，失败 {failed_count} 个"
        yield event.chain_result([Plain(summary)])

    @filter.llm_tool(name="delete_group_files")
    async def delete_group_files(self, event: AstrMessageEvent, confirmed: str = "false") -> MessageEventResult:
        """删除当前群聊根目录下的所有文件。扫描并删除群文件根目录中所有非文件夹文件，
删除完成后自动以合并转发形式发送详细删除报告（含文件名、大小、成功/失败状态）。

适用场景：用户要求清理群文件、删除群文件、清空群文件目录等。

Args:
    confirmed(string): 用户是否已确认删除操作，"true"表示已确认，"false"表示仅预览（默认）
"""
        gid = self._get_group_id(event)
        if not gid:
            yield event.chain_result([Plain("❌ 该功能仅在群聊中可用")])
            return

        sender_id = self._get_sender_id(event)

        # 权限校验
        if not await self._check_is_admin(int(gid), sender_id):
            yield event.chain_result([Plain("❌ 权限不足：仅群主和管理员可以执行此操作")])
            return

        is_confirmed = str(confirmed).strip().lower() in ("true", "yes", "确认", "1", "是")

        if not is_confirmed:
            # 第一阶段：预览
            entries, _ = await self._delete_all_files(int(gid), dry_run=True)
            if not entries:
                yield event.chain_result([Plain("群根目录暂无文件")])
                return
            total_size = sum(e.get("file_size", 0) for e in entries)
            preview = f"⚠️ 群 {gid} 根目录共 {len(entries)} 个文件（{_format_file_size(total_size)}），前20个：\n"
            for i, e in enumerate(entries[:20]):
                preview += f"  {i+1}. {e['file_name']} [{_format_file_size(e.get('file_size', 0))}]\n"
            if len(entries) > 20:
                preview += f"  ... 还有 {len(entries) - 20} 个\n"
            preview += '请输入"确认删除"来执行删除操作。'
            yield event.chain_result([Plain(preview)])
        else:
            # 第二阶段：执行删除
            yield event.chain_result([Plain("⏳ 正在执行删除...")])

            entries, logs = await self._delete_all_files(int(gid))
            if not entries:
                yield event.chain_result([Plain(logs[0])])
                return

            deleted_count = sum(1 for e in entries if e["status"] == "deleted")
            failed_count = sum(1 for e in entries if e["status"] == "failed")

            sender_name = getattr(event, "sender_name", None) or sender_id
            file_names = [e["file_name"] for e in entries if e["status"] == "deleted"]
            add_history_entry(int(gid), sender_name, deleted_count, failed_count, file_names)

            nodes = _build_forward_nodes(entries, int(gid))
            try:
                await self._send_group_forward_msg(int(gid), nodes)
            except Exception as e:
                logger.error(f"[LLM删除] 发送合并转发失败: {e}")

            summary = f"删除完成：成功 {deleted_count} 个"
            if failed_count:
                summary += f"，失败 {failed_count} 个"
            yield event.chain_result([Plain(summary)])

    @filter.llm_tool(name="check_group_files")
    async def check_group_files(self, event: AstrMessageEvent) -> MessageEventResult:
        """查看当前群聊根目录下的文件列表。返回每个文件的名称和大小信息。

适用场景：用户询问群文件有哪些、查看群文件列表、了解群文件情况等。

Args:
"""
        gid = self._get_group_id(event)
        if not gid:
            yield event.plain_result("无法获取群聊信息，请在群聊中使用此功能。")
            return

        try:
            async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=10) as client:
                resp = await client.post("/get_group_root_files", json={"group_id": int(gid)})
                data = resp.json()
                if data.get("status") != "ok":
                    yield event.plain_result(f"获取文件列表失败：{data.get('msg', '未知错误')}")
                    return
                files = extract_file_list(data)
                if not files:
                    yield event.plain_result("当前群根目录没有文件。")
                    return

                lines = [f"群 {gid} 根目录文件列表（共 {len(files)} 个）："]
                for f in files:
                    fname = f.get("file_name", "未知")
                    fsize = _format_file_size(f.get("file_size", 0))
                    lines.append(f"- {fname}  [{fsize}]")
                yield event.plain_result("\n".join(lines))
        except (httpx.RequestError, json.JSONDecodeError) as e:
            yield event.plain_result(f"获取文件列表请求失败：{e}")

    @filter.command("测文件")
    async def test_file(self, event: AstrMessageEvent):
        gid = self._get_group_id(event)
        if not gid:
            yield event.chain_result([Plain("❌ 请在群聊中使用")])
            return
        yield event.chain_result([Plain(f"HTTP 地址: {HTTP_BASE}")])
        try:
            async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=10) as client:
                resp = await client.post("/get_group_root_files", json={"group_id": int(gid)})
                data = resp.json()
                if data.get("status") != "ok":
                    yield event.chain_result([Plain(f"❌ API 返回错误：{data.get('msg')}")])
                    return
                raw = json.dumps(data, ensure_ascii=False)[:500]
                files = extract_file_list(data)
                folders = data.get("data", {}).get("folders") or data.get("folders") or []
                msg = f"✅ 原始返回片段:\n{raw}\n\n解析到的文件数: {len(files)}\n文件夹数: {len(folders)}"
                if files:
                    msg += "\n文件列表:"
                    for f in files[:10]:
                        msg += f"\n- {f.get('file_name')} (id={f.get('file_id')})"
                yield event.chain_result([Plain(msg.strip())])
        except (httpx.RequestError, json.JSONDecodeError) as e:
            yield event.chain_result([Plain(f"❌ HTTP 请求失败：{type(e).__name__}: {e}")])