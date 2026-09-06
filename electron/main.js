// PaiVoice Electron 主进程
// 职责：
//   1. 一键启动：拉起完整后端（server + local_voice），再打开拨号页窗口
//      - 打包态：跑 PyInstaller 产物 server.exe / local_voice.exe
//      - 开发态（dist 里没有 exe）：直接用 .venv 的 python 跑源码，无需先 py:build
//   2. 隐私加固：数据目录指向 %APPDATA%\PaiVoice（PAIVOICE_DATA_DIR），
//      默认只绑 127.0.0.1（PAIVOICE_HOST），并把启动参数注入子进程 env
//   3. 应用退出时回收子进程（Python 会被拉起成孤儿进程，必须主动杀掉；
//      PyInstaller onefile 有 bootloader 子进程，Windows 上要按进程树杀）
"use strict";
const { app, BrowserWindow, dialog, Menu } = require("electron");
const path = require("path");
const { spawn, execSync } = require("child_process");
const fs = require("fs");

const PORT = 8780; // 与后端 server.py 默认端口一致
const APP_ROOT = path.join(__dirname, ".."); // 仓库根（开发态）；打包后 = resources\app（asar:false）

// ---------- 单实例锁：防止两个实例抢 8780 端口 ----------
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });
  main();
}

// ---------- 路径解析：打包后（resources/app）与开发时（仓库内）都适用 ----------
function isPackaged() {
  return !!(app && app.isPackaged);
}
function exePath() {
  // asar:false 时 electron-builder 把仓库内容原样放到 resources\app\；
  // 这里优先取「打包目录」，找不到再退回仓库 dist/（开发态，兼容直接跑 exe）
  const cands = [
    path.join(process.resourcesPath, "app", "dist"),   // win: resources\app\dist
    path.join(process.resourcesPath, "dist"),          // 兜底
    path.join(APP_ROOT, "dist"),
  ];
  for (const c of cands) {
    if (fs.existsSync(path.join(c, "server.exe"))) return c;
  }
  return cands[0];
}

let backend = null; // { procs: [...] }

function pickModelDir(dir) {
  // 模型目录：优先打包内 resources\models（installer 随包分发），
  // 再仓库根 models/（本机开发场景），最后桌面版数据目录（手动放模型的兜底）。
  // 注意：LOCAL_VOICE_MODEL_DIR 默认是「仓库根/models」，PyInstaller 打包后仓库路径不存在，
  // 所以必须显式指定，否则边车会因找不到模型退出。
  const cands = [
    path.join(APP_ROOT, "models"),                     // 开发：仓库根 models/
    path.join(process.resourcesPath, "models"),        // 打包：extraResources 的 models/
    path.join(dir, "..", "models"),                    // 打包（旧布局兜底）：dist\..\models
    path.join(app.getPath("userData"), "models"),      // 桌面版数据目录
  ];
  const found = cands.find((p) => {
    try { return fs.existsSync(path.join(p, "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17")); } catch (_) { return false; }
  });
  if (!found) {
    // 找不到模型就交给 local_voice.py 自己报（它会用 LOCAL_VOICE_MODEL_DIR 或默认，并提示先下载）
    console.log("[main] WARNING: no sherpa models found in candidate dirs:", cands);
  }
  return found;
}

function spawnOne(cmd, args, label, env) {
  const p = spawn(cmd, args, { env, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
  p.stdout.on("data", (d) => process.stdout.write(`[${label}] ` + d));
  p.stderr.on("data", (d) => process.stderr.write(`[${label}] ` + d));
  return p;
}

function spawnBackend() {
  const dir = exePath();
  const serverExe = path.join(dir, "server.exe");
  const venvPython = path.join(APP_ROOT, ".venv", "Scripts", "python.exe");
  // 开发态没有 PyInstaller 产物时直接跑源码（省掉每次 py:build）
  const useSource = !fs.existsSync(serverExe) && fs.existsSync(venvPython);

  const env = Object.assign({}, process.env, {
    // 隐私/隔离：所有用户数据（SQLite 记忆 / persona / api_profiles）放应用数据目录，不进程序目录
    PAIVOICE_DATA_DIR: app.getPath("userData"),
    // 默认只绑回环，防止局域网内其他设备直连本服务
    PAIVOICE_HOST: "127.0.0.1",
    PAIVOICE_PORT: String(PORT),
    // 本地语音边车（模型目录已定位）
    LOCAL_VOICE_MODEL_DIR: pickModelDir(dir),
    // 前端页面根 / 图标目录（server.exe 是 PyInstaller 单文件，找不到仓库文件；显式指过去）
    PAIVOICE_WEB_ROOT: path.join(APP_ROOT, "packages", "web-client"),
    PAIVOICE_ICON_DIR: path.join(APP_ROOT, "assets", "icons"),
    // 默认不开云端/归档/Supabase：用户需要时再在设置面板填（运行时热改，不回写 .env）
    PAIVOICE_ASR_PROVIDER: "local",
    PAIVOICE_TTS_PROVIDER: "local",
    PAIVOICE_ARCHIVE_URL: "",
    PAIVOICE_SB_URL: "",
    PAIVOICE_SB_KEY: "",
  });
  // 打包机器上没有 git 仓库 → L2 项目记忆自动回退 default 桶
  env.PAIVOICE_GIT_ROOT = "";

  const procs = [];
  if (useSource) {
    const serverEntry = path.join(APP_ROOT, "packages", "realtime-core", "server.py");
    const voiceEntry = path.join(APP_ROOT, "packages", "local-voice", "local_voice.py");
    console.log("[main] dev mode: no dist/server.exe, running python source from .venv");
    procs.push(spawnOne(venvPython, [voiceEntry], "local-voice", env));
    procs.push(spawnOne(venvPython, [serverEntry], "server", env));
  } else {
    const voiceExe = path.join(dir, "local_voice.exe");
    if (fs.existsSync(voiceExe)) {
      procs.push(spawnOne(voiceExe, [], "local-voice", env));
    }
    procs.push(spawnOne(serverExe, [], "server", env));
  }
  backend = { procs, dir };
  return procs[procs.length - 1]; // server（最后的进程）负责端口监听
}

function stopBackend() {
  if (!backend) return;
  for (const p of backend.procs) {
    try {
      if (process.platform === "win32" && p.pid) {
        // PyInstaller onefile 会再 fork 真正的子进程，只 kill 父进程会留孤儿占着 8780 端口；
        // taskkill /T 按进程树整组杀干净
        execSync(`taskkill /pid ${p.pid} /T /F`, { windowsHide: true, stdio: "ignore" });
      } else {
        p.kill();
      }
    } catch (_) {}
  }
  backend = null;
}

let win = null;
function createWindow() {
  const iconFile = path.join(APP_ROOT, "assets", "icons", "paivoice-icon.ico");
  win = new BrowserWindow({
    width: 1200,
    height: 800,
    // 开发态 electron.exe 没有内嵌图标，必须显式指给窗口/任务栏；打包态 exe 已内嵌，此设置也无害
    icon: fs.existsSync(iconFile) ? iconFile : undefined,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.loadURL("http://127.0.0.1:" + PORT + "/");
  win.on("closed", () => { win = null; });
}

function main() {
  // 去掉 Electron 默认菜单栏（File/Edit/View/Window/Help 那条），桌面应用不需要
  Menu.setApplicationMenu(null);
  app.whenReady().then(() => {
    const srv = spawnBackend();
    srv.on("error", (err) => {
      dialog.showErrorBox("后端启动失败", String(err) +
        (fs.existsSync(path.join(APP_ROOT, ".venv"))
          ? ""
          : "\n\n缺少 .venv 且 dist/server.exe 不存在：先运行 scripts/run.ps1 装依赖，或 npm run py:build。"));
    });
    // 等后端端口就绪再开窗口（最多 15 秒）
    const t0 = Date.now();
    const wait = setInterval(() => {
      try {
        execSync(`netstat -ano | findstr :${PORT}`, { encoding: "utf8", windowsHide: true });
        clearInterval(wait);
        createWindow();
      } catch (e) {
        if (Date.now() - t0 > 15000) {
          clearInterval(wait);
          dialog.showErrorBox("后端未就绪", `端口 ${PORT} 未在 15 秒内监听，请查看命令行日志。`);
        }
      }
    }, 500);

    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on("window-all-closed", () => {
    stopBackend();
    if (process.platform !== "darwin") app.quit();
  });
  app.on("before-quit", stopBackend);
  process.on("exit", stopBackend);
}
