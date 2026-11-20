#!/usr/bin/env python3
"""Official block-titlebar plugin for Block (blockmd)."""

import json
import os
import re
import sys


STORAGE_KEY = "hidden_slots"
SETTINGS_PAGE_SURFACE = "settings_page"
SETTINGS_SECTION_SURFACE = "settings_section:titlebar-icons"
SUPPORTED_SURFACES = {SETTINGS_PAGE_SURFACE, SETTINGS_SECTION_SURFACE}
LOCKED_SLOT_IDS = {"more"}
CHINESE_TEXT = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")

BUILTIN_SLOTS = (
    ("feedback", "反馈", "Feedback", False),
    ("ai", "AI 助手", "AI Assistant", False),
    ("send_all", "统一发送批注", "Send All Annotations", False),
    ("wrap", "自动换行", "Word Wrap", False),
    ("zoom", "缩放", "Zoom", False),
    ("reveal", "在文件夹中显示", "Reveal in Folder", False),
    ("more", "页面操作", "Page Actions", True),
)


class HostRPCError(RuntimeError):
    """JSON-RPC error returned by the Block host."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class HostBridge:
    """Line-delimited JSON-RPC 2.0 bridge between plugin and Block host."""

    def __init__(self):
        self._seq = 1000

    def send(self, obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def call_host(self, method, params=None):
        self._seq += 1
        req_id = f"p-{self._seq}"
        self.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        while True:
            line = sys.stdin.readline()
            if not line:
                raise RuntimeError("stdin closed while awaiting host response")
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if str(msg.get("id")) == req_id:
                if msg.get("error"):
                    err = msg["error"]
                    raise HostRPCError(int(err.get("code", -32000)), str(err.get("message", "host error")))
                return msg.get("result", {})

    def serve(self, handler, localize_error):
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            req = json.loads(line)
            method = req.get("method")
            req_id = req.get("id")
            try:
                result = handler(self, method, req.get("params") or {})
                self.send({"jsonrpc": "2.0", "id": req_id, "result": result})
                if method == "plugin.shutdown":
                    break
            except Exception as exc:
                self.send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": localize_error(str(exc))},
                })


class TitlebarPlugin:
    """Manages titlebar icon visibility for Block."""

    def __init__(self):
        self.plugin_id = "block-titlebar"
        self.data_dir = ""
        self.downloads_dir = os.path.expanduser("~/Downloads")
        self.locale = "zh"
        self.kv_tooltip = False
        self.cached_hidden = None

    def _is_en(self):
        return str(self.locale or "").lower().startswith("en")

    def _tr(self, zh, en):
        return en if self._is_en() else zh

    def _localize_error(self, message):
        text = str(message or "").strip()
        if self._is_en() and CHINESE_TEXT.search(text):
            return "Operation failed"
        return text or self._tr("操作失败", "Operation failed")

    @staticmethod
    def _control_id(slot_id):
        return f"show_{slot_id}"

    @staticmethod
    def _is_locked(item):
        return bool(item.get("locked")) or str(item.get("id") or "") in LOCKED_SLOT_IDS

    @staticmethod
    def _clean_hidden(raw_ids):
        if not isinstance(raw_ids, list):
            return []
        cleaned = {
            str(slot_id).strip()
            for slot_id in raw_ids
            if isinstance(slot_id, str) and str(slot_id).strip() and str(slot_id).strip() not in LOCKED_SLOT_IDS
        }
        return sorted(cleaned)

    def _fallback_items(self):
        hidden_set = set(self.cached_hidden or [])
        items = []
        for slot_id, zh_label, en_label, locked in BUILTIN_SLOTS:
            items.append({
                "id": slot_id,
                "label": en_label if self._is_en() else zh_label,
                "locked": locked,
                "hidden": False if locked else (slot_id in hidden_set),
            })
        return items

    @staticmethod
    def _normalize_items(raw_items):
        items = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            slot_id = str(raw.get("id") or "").strip()
            if not slot_id:
                continue
            locked = bool(raw.get("locked")) or slot_id in LOCKED_SLOT_IDS
            label = str(raw.get("label") or slot_id)
            items.append({
                "id": slot_id,
                "label": label,
                "locked": locked,
                "hidden": False if locked else bool(raw.get("hidden")),
            })
        return items

    def _get_items(self, bridge):
        try:
            res = bridge.call_host("host.titlebar.get_items", {})
        except HostRPCError as exc:
            if exc.code == -32601:
                return self._fallback_items()
            raise
        raw_items = res.get("items") if isinstance(res, dict) else None
        if isinstance(raw_items, list):
            return self._normalize_items(raw_items)
        return self._fallback_items()

    def _refresh_other_surface(self, bridge, current_surface):
        other = SETTINGS_SECTION_SURFACE if current_surface == SETTINGS_PAGE_SURFACE else SETTINGS_PAGE_SURFACE
        try:
            bridge.call_host("host.ui.refresh", {"surface": other})
        except HostRPCError:
            pass

    def _set_hidden(self, bridge, hidden_ids, current_surface=None):
        cleaned = self._clean_hidden(hidden_ids)
        self.cached_hidden = list(cleaned)
        updated_items = None
        try:
            res = bridge.call_host("host.titlebar.set_hidden", {"hidden": cleaned})
            if isinstance(res, dict) and isinstance(res.get("items"), list):
                updated_items = self._normalize_items(res["items"])
        except HostRPCError as exc:
            if exc.code != -32601:
                raise
        bridge.call_host("host.storage.set", {"key": STORAGE_KEY, "value": cleaned})
        if current_surface:
            self._refresh_other_surface(bridge, current_surface)
        if updated_items is not None:
            return updated_items
        return self._get_items(bridge)

    def _current_values(self, items):
        return {
            self._control_id(item["id"]): not bool(item.get("hidden"))
            for item in items
            if not self._is_locked(item)
        }

    def _build_page(self, surface, items, status=""):
        card_items = []
        locked_rows = []
        for item in items:
            if self._is_locked(item):
                row = {
                    "label": str(item["label"]),
                    "value": self._tr("始终显示", "Always shown"),
                }
                if self.kv_tooltip:
                    row["tooltip"] = self._tr(
                        "该入口始终显示在顶栏右上角，不可隐藏",
                        "This item is always shown in the titlebar and cannot be hidden",
                    )
                locked_rows.append(row)
                continue
            card_items.append({
                "kind": "switch",
                "id": self._control_id(item["id"]),
                "label": str(item["label"]),
                "value": not bool(item.get("hidden")),
            })

        if locked_rows:
            card_items.append({"kind": "kv", "rows": locked_rows})

        card_items.append({
            "kind": "actions",
            "buttons": [
                {
                    "id": "show_all",
                    "label": self._tr("全部显示", "Show all"),
                    "variant": "secondary",
                }
            ],
        })

        title = (
            self._tr("顶栏右上角图标", "Titlebar Icons")
            if surface == SETTINGS_SECTION_SURFACE
            else self._tr("顶栏图标", "Titlebar Icons")
        )
        card_title = "" if surface == SETTINGS_SECTION_SURFACE else self._tr("顶栏右上角图标", "Titlebar Icons")
        return {
            "title": title,
            "status": status,
            "cards": [
                {
                    "id": "titlebar-icons",
                    "title": card_title,
                    "items": card_items,
                }
            ],
        }

    def _validate_surface(self, params):
        surface = str((params or {}).get("surface") or SETTINGS_PAGE_SURFACE).strip() or SETTINGS_PAGE_SURFACE
        if surface not in SUPPORTED_SURFACES:
            raise RuntimeError(self._tr(f"不支持的界面: {surface}", f"Unsupported surface: {surface}"))
        return surface

    def handle(self, bridge, method, params):
        if method == "plugin.initialize":
            self.plugin_id = str(params.get("plugin_id") or "block-titlebar")
            self.data_dir = str(params.get("data_dir") or "")
            dl = str(params.get("downloads_dir") or "").strip()
            self.downloads_dir = dl or os.path.expanduser("~/Downloads")
            self.locale = str(params.get("locale") or "zh")
            caps = set(params.get("ui_capabilities") or [])
            self.kv_tooltip = "kv_tooltip" in caps

            stored = bridge.call_host("host.storage.get", {"key": STORAGE_KEY})
            if isinstance(stored, dict) and stored.get("found") and isinstance(stored.get("value"), list):
                saved_hidden = self._clean_hidden(stored["value"])
                self.cached_hidden = list(saved_hidden)
                try:
                    bridge.call_host("host.titlebar.set_hidden", {"hidden": saved_hidden})
                except HostRPCError as exc:
                    if exc.code != -32601:
                        raise
            else:
                self.cached_hidden = None
            return {"ready": True}

        if method == "plugin.render":
            surface = self._validate_surface(params)
            items = self._get_items(bridge)
            values = params.get("values")
            if isinstance(values, dict) and values:
                current_hidden = {
                    item["id"]
                    for item in items
                    if item.get("hidden") and not self._is_locked(item)
                }
                next_hidden = set(current_hidden)
                touched = False
                for item in items:
                    if self._is_locked(item):
                        continue
                    ctrl_id = self._control_id(item["id"])
                    if ctrl_id in values:
                        touched = True
                        if bool(values[ctrl_id]):
                            next_hidden.discard(item["id"])
                        else:
                            next_hidden.add(item["id"])
                if touched and next_hidden != current_hidden:
                    items = self._set_hidden(bridge, sorted(next_hidden), current_surface=surface)
                accepted = {
                    self._control_id(item["id"]): not bool(item.get("hidden"))
                    for item in items
                    if not self._is_locked(item) and self._control_id(item["id"]) in values
                }
            else:
                accepted = self._current_values(items)
            return {"page": self._build_page(surface, items), "values": accepted}

        if method == "plugin.action":
            surface = self._validate_surface(params)
            action_id = str(params.get("action_id") or "")
            if action_id == "show_all":
                items = self._set_hidden(bridge, [], current_surface=surface)
                status = self._tr("已显示全部顶栏图标", "All titlebar icons are now shown")
                return {
                    "ok": True,
                    "tone": "success",
                    "values": self._current_values(items),
                    "page": self._build_page(surface, items, status=status),
                    "status": status,
                }
            items = self._get_items(bridge)
            status = self._tr("未知操作", "Unknown action")
            return {
                "ok": False,
                "tone": "warning",
                "values": self._current_values(items),
                "page": self._build_page(surface, items),
                "status": status,
            }

        if method == "plugin.event":
            event_name = str(params.get("event") or "")
            if event_name == "locale.changed" and params.get("locale"):
                self.locale = str(params["locale"])
                return {"handled": True}
            return {"handled": False}

        if method == "plugin.shutdown":
            return {"ok": True}

        raise RuntimeError(self._tr(f"不支持的方法: {method}", f"Unsupported method: {method}"))


def main():
    plugin = TitlebarPlugin()
    HostBridge().serve(plugin.handle, plugin._localize_error)


if __name__ == "__main__":
    main()
