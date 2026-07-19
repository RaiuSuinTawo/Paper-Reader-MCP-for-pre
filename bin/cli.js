#!/usr/bin/env node
/**
 * paper-mcp 启动包装器。
 *
 * 本包是一个 Python MCP Server(FastMCP + httpx)。这里用 Node 包装,让它能通过
 * `npx @femio/paper-mcp` 启动:优先用 uv 拉起(自动准备 Python 与 mcp/httpx 依赖),
 * 无 uv 时回退到系统 python(需已装 mcp、httpx)。
 *
 * MCP 走 stdio(stdin/stdout 是 JSON-RPC 协议通道),因此这里用 stdio:'inherit'
 * 把子进程的三个标准流透传给客户端,并转发退出码与终止信号。
 *
 * 可用环境变量:
 *   MINERU_API_TOKEN   —— 必填,MinerU 云端 API Token
 *   PAPER_MCP_PYTHON   —— 指定 Python 版本(uv --python,默认 3.12)或解释器路径(回退时)
 *   PAPER_MCP_NO_UV=1  —— 强制不使用 uv,直接用系统 python
 */
"use strict";

const { spawn, spawnSync } = require("child_process");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const ENTRY = path.join(ROOT, "paper-mcp.py");
const DEPS = ["mcp>=1.2.0", "httpx>=0.27.0"]; // Python 服务器所需依赖
const PYREQ = process.env.PAPER_MCP_PYTHON || "3.12";

function has(cmd) {
  // uv/python/python3 均为原生可执行文件(Windows 上 CreateProcess 会自动补 .exe),
  // 因此无需 shell 即可探测;命令不存在时 r.error 为 ENOENT。
  const r = spawnSync(cmd, ["--version"], { stdio: "ignore" });
  return !r.error && r.status === 0;
}

function launch(cmd, args) {
  const child = spawn(cmd, args, { stdio: "inherit", env: process.env });

  const forward = (sig) => {
    try { child.kill(sig); } catch (_) { /* 子进程可能已退出 */ }
  };
  process.on("SIGINT", () => forward("SIGINT"));
  process.on("SIGTERM", () => forward("SIGTERM"));

  child.on("error", (err) => {
    console.error(`[paper-mcp] 无法启动 ${cmd}:${err.message}`);
    process.exit(127);
  });
  child.on("exit", (code, signal) => {
    if (signal) process.kill(process.pid, signal);
    else process.exit(code == null ? 1 : code);
  });
}

const forwarded = process.argv.slice(2);
const useUv = process.env.PAPER_MCP_NO_UV !== "1" && has("uv");

if (useUv) {
  // uv 会按需下载/复用 Python,并在临时环境中叠加 mcp、httpx(首次运行可能稍慢,之后走缓存)。
  const args = ["run", "--python", PYREQ];
  for (const d of DEPS) args.push("--with", d);
  args.push("python", ENTRY, ...forwarded);
  launch("uv", args);
} else {
  const py = has("python3") ? "python3" : has("python") ? "python" : null;
  if (!py) {
    console.error(
      "[paper-mcp] 未找到 uv,也未找到 Python 3.10+。\n" +
      "  推荐安装 uv(会自动管理 Python 与依赖):https://docs.astral.sh/uv/\n" +
      "  或安装 Python 3.10+ 并 `pip install mcp httpx`。"
    );
    process.exit(1);
  }
  if (process.env.PAPER_MCP_NO_UV !== "1") {
    console.error(`[paper-mcp] 未检测到 uv,回退用 ${py} 运行(需已 pip install mcp httpx)。建议安装 uv 以自动管理依赖。`);
  }
  launch(py, [ENTRY, ...forwarded]);
}
