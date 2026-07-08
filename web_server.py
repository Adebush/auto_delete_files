"""Web 管理面板服务端 — 端口 1655"""
import json
import httpx
from aiohttp import web
from pathlib import Path

from storage import load_history, load_config, save_config, add_history_entry

# HTTP_BASE 由 start_web_server() 在运行时注入，此处仅作类型占位
HTTP_BASE = ""
WEB_DIR = Path(__file__).parent / "web"

routes = web.RouteTableDef()


# ── 静态页面 ──────────────────────────────────

@routes.get("/")
async def index(_request):
    return web.FileResponse(WEB_DIR / "index.html")


# ── 群列表 ────────────────────────────────────

@routes.get("/api/groups")
async def api_groups(_request):
    try:
        async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=8) as client:
            resp = await client.post("/get_group_list")
            data = resp.json()
            groups = data.get("data", [])
            return web.json_response([
                {
                    "group_id": g.get("group_id"),
                    "group_name": g.get("group_name", str(g.get("group_id", "未知"))),
                }
                for g in groups
            ])
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ── 群文件列表 ────────────────────────────────

@routes.get("/api/files/{group_id}")
async def api_files(request: web.Request):
    group_id = request.match_info["group_id"]
    try:
        async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=10) as client:
            resp = await client.post(
                "/get_group_root_files",
                json={"group_id": int(group_id)},
            )
            data = resp.json()
            if data.get("status") != "ok":
                return web.json_response(
                    {"error": data.get("msg", "获取失败")}, status=400
                )

            files_raw = data.get("data", {}).get("files") or data.get("files") or []
            files = [
                {
                    "file_id": f.get("file_id"),
                    "file_name": f.get("file_name", "未知"),
                    "file_size": f.get("file_size", 0),
                    "busid": f.get("busid", 0),
                    "upload_time": f.get("upload_time", 0),
                    "uploader": f.get("uploader_name") or f.get("uploader", ""),
                    "is_folder": bool(f.get("is_folder")),
                }
                for f in files_raw
                if not f.get("is_folder") and f.get("busid") != 2
            ]
            return web.json_response({"files": files, "group_id": int(group_id)})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ── 删除文件（Web端） ──────────────────────────

@routes.post("/api/delete/{group_id}")
async def api_delete(request: web.Request):
    group_id = request.match_info["group_id"]
    try:
        body = await request.json()
        file_ids = body.get("file_ids", [])
        user_id = str(body.get("user_id", ""))
        file_names = body.get("file_names", [])  # 前端传入文件名列表，用于历史记录
        if not file_ids:
            return web.json_response({"error": "未选择文件"}, status=400)

        results = []
        deleted = 0
        failed = 0
        deleted_names = []
        async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=10) as client:
            for i, fid in enumerate(file_ids):
                resp = await client.post(
                    "/delete_group_file",
                    json={"group_id": int(group_id), "file_id": fid},
                )
                rdata = resp.json()
                fname = file_names[i] if i < len(file_names) else fid
                if rdata.get("status") == "ok":
                    deleted += 1
                    deleted_names.append(fname)
                    results.append({"file_id": fid, "file_name": fname, "status": "deleted"})
                else:
                    failed += 1
                    results.append({
                        "file_id": fid,
                        "file_name": fname,
                        "status": "failed",
                        "error": rdata.get("msg", "未知"),
                    })

        # 记录清理历史
        operator = user_id or "Web面板"
        add_history_entry(int(group_id), operator, deleted, failed, deleted_names)

        return web.json_response({
            "deleted": deleted,
            "failed": failed,
            "results": results,
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ── 清理历史 ──────────────────────────────────

@routes.get("/api/history")
async def api_history(_request):
    try:
        records = load_history()
        return web.json_response(records)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ── 系统配置 ──────────────────────────────────

@routes.get("/api/config")
async def api_get_config(_request):
    try:
        return web.json_response(load_config())
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@routes.post("/api/config")
async def api_save_config(request: web.Request):
    try:
        body = await request.json()
        cfg = load_config()
        if "auto_clean_time" in body:
            cfg["auto_clean_time"] = body["auto_clean_time"]
        if "auto_clean_day" in body:
            cfg["auto_clean_day"] = int(body["auto_clean_day"])
        save_config(cfg)
        return web.json_response({"status": "ok", "config": cfg})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ── 权限检查 ─────────────────────────────────

@routes.get("/api/permission/{group_id}/{user_id}")
async def api_permission(request: web.Request):
    group_id = request.match_info["group_id"]
    user_id = request.match_info["user_id"]
    try:
        async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=8) as client:
            resp = await client.post(
                "/get_group_member_info",
                json={"group_id": int(group_id), "user_id": int(user_id)},
            )
            data = resp.json()
            role = data.get("data", {}).get("role", "member")
            is_admin = role in ("owner", "admin")
            return web.json_response({
                "role": role,
                "is_admin": is_admin,
            })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ── 服务启动 ──────────────────────────────────

_app_runner = None


async def start_web_server(http_base: str = "http://127.0.0.1:3000", port: int = 1655):
    global HTTP_BASE, _app_runner
    HTTP_BASE = http_base

    app = web.Application()
    app.add_routes(routes)
    _app_runner = web.AppRunner(app)
    await _app_runner.setup()
    site = web.TCPSite(_app_runner, "0.0.0.0", port)
    await site.start()
    print(f"[WebUI] 管理面板已启动: http://0.0.0.0:{port}")


async def stop_web_server():
    global _app_runner
    if _app_runner:
        await _app_runner.cleanup()
        _app_runner = None
