// 用 AI 把 release-notes.md 里的提交列表改写成正式的中英双语更新说明，原地覆盖。
// 顺序：Mac Magpie agy-cli/gemini-3.8-flash（Mac 在线才用）→ VPS Magpie gpt-6-luna → VPS Magpie deepseek-flash（付费）。
// 全部失败就退出 1，停止发版，不用拼凑文案。不要用 Google AI Studio 的 Gemini key。
// 本文件与 Block 主仓 scripts/release/release-notes.mjs 的模型顺序保持一致；各插件仓库共用同一份。
import { readFileSync, writeFileSync } from "node:fs";

const PROVIDERS = [
  { model: "agy-cli/gemini-3.8-flash", url: "http://100.118.27.43:3425/v1/chat/completions", gate: "MAC_MAGPIE_REACHABLE", gateReason: "Mac 不在线", timeoutMs: 120_000 },
  { model: "gpt-6-luna", url: "http://100.115.90.80:3425/v1/chat/completions", gate: "MAGPIE_REACHABLE", gateReason: "CI 未连通 VPS Magpie", timeoutMs: 90_000 },
  { model: "deepseek-flash", url: "http://100.115.90.80:3425/v1/chat/completions", gate: "MAGPIE_REACHABLE", gateReason: "CI 未连通 VPS Magpie", timeoutMs: 90_000 },
];
const CJK = /[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]/;
const HAN = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/;
const EN_SECTIONS = ["New", "Improvements", "Fixes", "Other"];
const ZH_SECTIONS = ["新功能", "改进", "修复", "其他"];

const path = process.argv[2] || "release-notes.md";
const manifest = JSON.parse(readFileSync("manifest.json", "utf8"));
const nameZh = manifest.name?.zh || manifest.id;
const nameEn = manifest.name?.en || manifest.id;
const summaryZh = manifest.summary?.zh || "";
const summaryEn = manifest.summary?.en || "";

const commits = readFileSync(path, "utf8")
  .split("\n")
  .map((line) => line.match(/^- (.+?) \(`[0-9a-f]{7,40}`\)\s*$/))
  .filter(Boolean)
  .map((m) => m[1].trim());

if (!commits.length) {
  writeFileSync(path, "## English\n\n### Other\n\n- This is a maintenance release with no user-facing changes.\n\n---\n\n## 中文\n\n### 其他\n\n- 本次为维护版本，没有面向用户的变化。\n");
  console.log("没有可写的提交，写入固定的维护版本说明。");
  process.exit(0);
}

const system = `你是 Block 笔记应用插件「${nameZh}」（${nameEn}）的发版编辑。插件简介：${summaryZh}（${summaryEn}）。只根据给定的提交写这一版的正式更新说明，每条提交恰好一条，不能合并掉，也不能漏掉。构建、CI、发版脚本、打包可以归入 Other，但每条仍要单独写一句。

提交标题是开发者写的英文简写，不能照抄。先弄清这条提交对使用者意味着什么，再分别写英文和中文：

- 英文：完整、正式的句子，像正规产品的发版说明，主语清楚，例如 "Sync now resumes an interrupted upload instead of starting over."。
- 中文：把同一条意思用正式、通顺的简体中文写出来，具体说清改了什么、在哪里能感受到。不要逐词硬译，不要写「修复了一处细节问题」这类空泛套话。
- 两种语言说的是同一件事，信息量一致。

只输出这些部分，不要前言、日期或代码围栏：

## English
### New
### Improvements
### Fixes
### Other

---

## 中文
### 新功能
### 改进
### 修复
### 其他

没有条目的分类整段省略。两边出现的分类要一一对应，同一分类里的条目数量和顺序一致。英文在上，中文在下。每条以 \`- \` 开头，一行一条，英文和中文都用句号结尾。同一行不要中英混写。不要提交号、文件名、命令或内部框架名。`;

function validate(raw) {
  const text = String(raw || "").replace(/^```[a-z]*\n?|\n?```\s*$/g, "").trim();
  const parts = text.split(/^---\s*$/m);
  if (parts.length !== 2) throw new Error("没有用 --- 分成英文和中文两段");
  const [en, zh] = parts.map((p) => p.trim());
  if (!/^## English\s*$/m.test(en) || !/^## 中文\s*$/m.test(zh)) throw new Error("缺少 ## English 或 ## 中文");
  const read = (part, names) => {
    const out = [];
    let current = null;
    for (const line of part.split("\n").map((l) => l.trim()).filter(Boolean)) {
      if (line.startsWith("## ")) continue;
      const h = line.match(/^### (.+)$/);
      if (h) {
        if (!names.includes(h[1].trim())) throw new Error(`未知分类 ${h[1]}`);
        current = { name: h[1].trim(), items: [] };
        out.push(current);
        continue;
      }
      if (!line.startsWith("- ") || !current) throw new Error(`多余的行 ${line}`);
      current.items.push(line.slice(2).trim());
    }
    return out;
  };
  const enSec = read(en, EN_SECTIONS);
  const zhSec = read(zh, ZH_SECTIONS);
  if (enSec.length !== zhSec.length) throw new Error("中英分类数量不一致");
  let total = 0;
  enSec.forEach((s, i) => {
    const z = zhSec[i];
    if (ZH_SECTIONS[EN_SECTIONS.indexOf(s.name)] !== z.name) throw new Error("中英分类不对应");
    if (s.items.length !== z.items.length || !s.items.length) throw new Error(`分类 ${s.name} 条目数量不一致`);
    for (const item of s.items) if (CJK.test(item)) throw new Error(`英文行含中文：${item}`);
    for (const item of z.items) if (!HAN.test(item)) throw new Error(`中文行没有中文：${item}`);
    total += s.items.length;
  });
  if (total !== commits.length) throw new Error(`条目数 ${total}/${commits.length}`);
  const render = (title, secs) => [`## ${title}`, ...secs.flatMap((s) => ["", `### ${s.name}`, "", ...s.items.map((it) => `- ${it}`)])].join("\n");
  return `${render("English", enSec)}\n\n---\n\n${render("中文", zhSec)}\n`;
}

async function tryProvider({ model, url, timeoutMs }) {
  const asked = `These are the only commits in this release of the ${nameEn} plugin (${commits.length}):\n\n${commits.map((c) => `- ${c}`).join("\n")}`;
  let lastRaw = "";
  let lastError = "没有生成结果";
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    const messages = [{ role: "system", content: system }, { role: "user", content: asked }];
    if (lastRaw) {
      messages.push({ role: "assistant", content: lastRaw });
      messages.push({ role: "user", content: `The previous draft failed validation (${lastError}). Rewrite it: one bullet per commit, one language per line, English above ---, Simplified Chinese below. Output markdown only.` });
    }
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        redirect: "error",
        signal: AbortSignal.timeout(timeoutMs),
        body: JSON.stringify({ model, temperature: 0.2, messages }),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const content = data?.choices?.[0]?.message?.content;
      lastRaw = typeof content === "string" ? content : Array.isArray(content) ? content.map((p) => p?.text || "").join("") : "";
      return validate(lastRaw);
    } catch (err) {
      lastError = err?.name === "TimeoutError" ? "请求超时" : String(err?.message || err).slice(0, 160);
      if (!lastRaw) break;
    }
  }
  throw new Error(lastError);
}

const failures = [];
for (const provider of PROVIDERS) {
  if (process.env[provider.gate] === "no") {
    failures.push(`${provider.model}：${provider.gateReason}`);
    continue;
  }
  try {
    const markdown = await tryProvider(provider);
    writeFileSync(path, markdown);
    console.log(`更新说明由 AI 模型 ${provider.model} 生成（${commits.length} 条提交）。`);
    process.exit(0);
  } catch (err) {
    failures.push(`${provider.model}：${err.message}`);
  }
}
console.log(`::error title=release-notes::AI 没有生成更新说明（${failures.join("；")}）。本次停止发版，不会用拼凑文案。`);
process.exit(1);
