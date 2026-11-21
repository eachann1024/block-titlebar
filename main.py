#!/usr/bin/env python3
"""Official block-titlebar plugin for Block (blockmd)."""

import json
import os
import re
import sys


STORAGE_KEY = "hidden_slots"
SETTINGS_PAGE_SURFACE = "settings_page"
SUPPORTED_SURFACES = {SETTINGS_PAGE_SURFACE}
LOCKED_SLOT_IDS = {"more"}
SOURCE_SLOT_IDS = ("wrap", "zoom", "reveal")
PREVIEW_SKIP_IDS = {"reveal"}
MARKDOWN_ORDER = ("plugin:", "feedback", "ai", "send_all", "more")
SOURCE_ORDER = ("wrap", "zoom", "plugin:", "feedback", "ai", "more")
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
        self.item_icons = False
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
            item = {
                "id": slot_id,
                "label": label,
                "locked": locked,
                "hidden": False if locked else bool(raw.get("hidden")),
            }
            if isinstance(raw.get("icon"), dict):
                item["icon"] = raw["icon"]
            items.append(item)
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

    def _set_hidden(self, bridge, hidden_ids):
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
        if updated_items is not None:
            return updated_items
        return self._get_items(bridge)

    def _current_values(self, items):
        return {
            self._control_id(item["id"]): not bool(item.get("hidden"))
            for item in items
            if not self._is_locked(item)
        }

    def _row_icon(self, item):
        icon = item.get("icon")
        return dict(icon) if self.item_icons and isinstance(icon, dict) else None

    @staticmethod
    def _rank(slot_id, order):
        for rank, key in enumerate(order):
            if slot_id == key or (key.endswith(":") and slot_id.startswith(key)):
                return rank
        return None

    def _strip_icons(self, item):
        icon = self._row_icon(item)
        if icon is None:
            return []
        label = str(item["label"])
        if item["id"] == "zoom" and icon.get("name"):
            return [
                {"name": "tb-minus", "tooltip": label},
                {"chip": "100%", "tooltip": label},
                {"name": "tb-plus", "tooltip": label},
            ]
        icon["tooltip"] = label
        return [icon]

    def _build_strip(self, items, order, label):
        visible = [
            item for item in items
            if not item.get("hidden") and item["id"] not in PREVIEW_SKIP_IDS and self._rank(item["id"], order) is not None
        ]
        visible.sort(key=lambda item: self._rank(item["id"], order))
        icons = []
        for item in visible:
            icons.extend(self._strip_icons(item))
        return {
            "kind": "icon_strip",
            "label": self._tr(f"{label} · {len(visible)} 个", f"{label} · {len(visible)}"),
            "icons": icons,
            "empty_text": self._tr("不显示图标", "No icons shown"),
        }

    def _build_preview_card(self, items):
        return {
            "id": "titlebar-preview",
            "title": self._tr("预览", "Preview"),
            "items": [
                self._build_strip(items, MARKDOWN_ORDER, "Markdown"),
                self._build_strip(items, SOURCE_ORDER, self._tr("源码模式", "Source")),
            ],
        }

    def _build_rows(self, items):
        rows = []
        locked_rows = []
        for item in items:
            icon = self._row_icon(item)
            if self._is_locked(item):
                row = {
                    "label": str(item["label"]),
                    "value": self._tr("始终显示", "Always shown"),
                }
                if icon:
                    row["icon"] = icon
                if self.kv_tooltip:
                    row["tooltip"] = self._tr(
                        "该入口始终显示在顶栏右上角，不可隐藏",
                        "This item is always shown in the titlebar and cannot be hidden",
                    )
                locked_rows.append(row)
                continue
            switch = {
                "kind": "switch",
                "id": self._control_id(item["id"]),
                "label": str(item["label"]),
                "value": not bool(item.get("hidden")),
            }
            if icon:
                switch["icon"] = icon
            rows.append(switch)
        if locked_rows:
            rows.append({"kind": "kv", "rows": locked_rows})
        return rows

    def _build_page(self, items, status=""):
        markdown_items = [item for item in items if item["id"] not in SOURCE_SLOT_IDS]
        source_items = [item for item in items if item["id"] in SOURCE_SLOT_IDS]
        actions = {
            "kind": "actions",
            "buttons": [
                {
                    "id": "show_all",
                    "label": self._tr("全部显示", "Show all"),
                    "variant": "secondary",
                }
            ],
        }
        title = self._tr("顶栏图标", "Titlebar Icons")
        cards = []
        if self.item_icons:
            cards.append(self._build_preview_card(items))
        cards.append({"id": "titlebar-markdown", "title": "Markdown", "items": self._build_rows(markdown_items)})
        source_rows = self._build_rows(source_items) + [actions]
        cards.append({"id": "titlebar-source", "title": self._tr("源码模式", "Source Mode"), "items": source_rows})
        return {"title": title, "status": status, "cards": cards}

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
            self.item_icons = "item_icons" in caps

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
            self._validate_surface(params)
            items = self._get_items(bridge)
            values = params.get("values")
            if isinstance(values, dict) and values:
                visible_ids = {item["id"] for item in items}
                current_hidden = {
                    item["id"]
                    for item in items
                    if item.get("hidden") and not self._is_locked(item)
                }
                preserved_hidden = {
                    slot_id
                    for slot_id in (self.cached_hidden or [])
                    if slot_id.startswith("plugin:") and slot_id not in visible_ids
                }
                next_hidden = set(current_hidden) | preserved_hidden
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
                if touched and next_hidden != (current_hidden | preserved_hidden):
                    items = self._set_hidden(bridge, sorted(next_hidden))
                accepted = {
                    self._control_id(item["id"]): not bool(item.get("hidden"))
                    for item in items
                    if not self._is_locked(item) and self._control_id(item["id"]) in values
                }
            else:
                accepted = self._current_values(items)
            return {"page": self._build_page(items), "values": accepted}

        if method == "plugin.action":
            self._validate_surface(params)
            action_id = str(params.get("action_id") or "")
            if action_id == "show_all":
                items = self._set_hidden(bridge, [])
                status = self._tr("已显示全部顶栏图标", "All titlebar icons are now shown")
                return {
                    "ok": True,
                    "tone": "success",
                    "values": self._current_values(items),
                    "page": self._build_page(items, status=status),
                    "status": status,
                }
            items = self._get_items(bridge)
            status = self._tr("未知操作", "Unknown action")
            return {
                "ok": False,
                "tone": "warning",
                "values": self._current_values(items),
                "page": self._build_page(items),
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
