// 从原版「鱼鱼找鸡 v1.0.0」生成本插件的 engine/ 目录：
//   · 删掉入口从不调用的残留：DeepSeek 搜索（会读 ~/.dsh 凭据）、litterbox 图床、猫猫云 VPN/系统代理；
//   · 浏览器支持 Linux（Chromium / Playwright headless_shell），root 下自动加 --no-sandbox；
//   · 浏览器 profile 目录可由 FISH_STATE_DIR 指定（放到 AstrBot 的 plugin_data，升级插件不丢）。
// 用法：node tools/build_engine.mjs <原版解压目录>
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const src = process.argv[2];
if (!src) throw new Error('用法：node tools/build_engine.mjs <原版目录>');
const out = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'engine');
fs.mkdirSync(out, { recursive: true });

let code = fs.readFileSync(path.join(src, 'src', 'fish-find-chicken.js'), 'utf8').replace(/\r\n/g, '\n');

function cut(startMarker, endMarker, label) {
  const a = code.indexOf(startMarker);
  if (a < 0) throw new Error(`找不到起点：${label}`);
  const b = endMarker === null ? code.length : code.indexOf(endMarker, a);
  if (b < 0) throw new Error(`找不到终点：${label}`);
  code = code.slice(0, a) + code.slice(b);
}
function replace(from, to, label) {
  if (!code.includes(from)) throw new Error(`找不到替换点：${label}`);
  code = code.replace(from, to);
}

// 1. 残留：DeepSeek 搜索 / 必应谷歌网页搜索（只给 inferAccounts 用，入口不调用）
replace("import { loadSearchConfig, deepseekSearch } from './web-search.js';\n", '', 'web-search import');
cut('/** 给 inferAccounts 用的搜索函数', 'export const ENGINE_IDS', 'createSearchFn 一段');
// 2. 残留：litterbox 临时图床（不受白名单约束）
cut('// ── 临时公网图床', '// ── 各引擎适配器', 'litterbox');
// 3. 残留：猫猫云 VPN / 改 Windows 系统代理，一直到文件末尾
cut('// ── 猫猫云：按需开 / 关 VPN', null, 'VPN');
replace("import net from 'node:net';\n", '', 'net import');
replace("import { spawn, execFileSync } from 'node:child_process';", "import { spawn, spawnSync } from 'node:child_process';", 'child_process import');

// 4. 浏览器候选：加 Linux / macOS
replace(
  "  'C:\\\\Program Files (x86)\\\\Google\\\\Chrome\\\\Application\\\\chrome.exe'\n];",
  "  'C:\\\\Program Files (x86)\\\\Google\\\\Chrome\\\\Application\\\\chrome.exe',\n" +
  "  '/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable',\n" +
  "  '/usr/bin/microsoft-edge', '/snap/bin/chromium',\n" +
  "  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',\n" +
  "  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'\n];",
  '浏览器候选'
);
// 5. profile 目录可配置
replace(
  "const BROWSER_PROFILE_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'state', 'fish-find-profile');",
  "const BROWSER_PROFILE_DIR = process.env.FISH_STATE_DIR\n" +
  "  ? path.join(process.env.FISH_STATE_DIR, 'browser-profile')\n" +
  "  : path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'state', 'fish-find-profile');",
  'profile 目录'
);
// 6. Linux root（Docker 里很常见）必须 --no-sandbox，/dev/shm 小也要处理
replace(
  "    '--disable-gpu',\n    '--no-first-run',",
  "    '--disable-gpu',\n" +
  "    ...(process.platform !== 'win32' && typeof process.getuid === 'function' && process.getuid() === 0 ? ['--no-sandbox'] : []),\n" +
  "    ...(process.platform === 'linux' ? ['--disable-dev-shm-usage'] : []),\n" +
  "    '--no-first-run',",
  '启动参数'
);
// 7. 关浏览器：只有 Windows 用 taskkill，其它系统杀进程组
replace(
  "  try {\n    const { spawnSync } = await import('node:child_process');\n    spawnSync('taskkill', ['/PID', String(state.proc.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true });\n  } catch {}\n  try { state.proc.kill(); } catch {}",
  "  if (process.platform === 'win32') {\n" +
  "    try { spawnSync('taskkill', ['/PID', String(state.proc.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true }); } catch {}\n" +
  "  } else {\n" +
  "    try { process.kill(-state.proc.pid, 'SIGKILL'); } catch {}\n" +
  "  }\n" +
  "  try { state.proc.kill('SIGKILL'); } catch {}",
  'closeBrowser'
);
// 非 Windows 下让浏览器自成进程组，上面才能一次杀干净
replace(
  "const proc = spawn(exePath, args, { stdio: 'ignore', windowsHide: true });",
  "const proc = spawn(exePath, args, { stdio: 'ignore', windowsHide: true, detached: process.platform !== 'win32' });",
  'spawn'
);

for (const word of ['deepseek', 'litterbox', 'execFileSync', 'vpnEnsure', 'Internet Settings']) {
  if (code.includes(word)) throw new Error(`还残留：${word}`);
}

const header = '// 本文件由 tools/build_engine.mjs 从「鱼鱼找鸡 v1.0.0」（MIT，© 2026 小鲸鱼）生成并精简。\n';
fs.writeFileSync(path.join(out, 'fish-find-chicken.js'), header + code);
// safe-fetch：Clash/mihomo 的 fake-ip 模式会把所有域名解析到 198.18.0.0/15（IPv6 是 2001:2::/48），
// 原版会把它当内网拦掉，导致树莓派/开了代理的电脑上一个引擎都跑不通。
// 设置环境变量 FISH_ALLOW_FAKE_IP=1 时放行这两个基准测试网段（引擎只访问写死的识图网站）。
let safe = fs.readFileSync(path.join(src, 'src', 'safe-fetch.js'), 'utf8').replace(/\r\n/g, '\n');
const safeFrom = "    if (parts[0] === 198 && parts[1] >= 18 && parts[1] <= 19) return true;";
if (!safe.includes(safeFrom)) throw new Error('找不到替换点：fake-ip v4');
safe = safe.replace(safeFrom, "    if (parts[0] === 198 && parts[1] >= 18 && parts[1] <= 19) return !FAKE_IP_OK;");
const safeFrom6 = "    if (h.startsWith('2001:2:') || h.startsWith('2001:10:') || h.startsWith('2001:20:')) return true;";
if (!safe.includes(safeFrom6)) throw new Error('找不到替换点：fake-ip v6');
safe = safe.replace(safeFrom6,
  "    if (h.startsWith('2001:2:')) return !FAKE_IP_OK;\n" +
  "    if (h.startsWith('2001:10:') || h.startsWith('2001:20:')) return true;");
const safeAnchor = 'export function isPrivateIp(ip) {';
if (!safe.includes(safeAnchor)) throw new Error('找不到替换点：isPrivateIp');
// fake-ip 时 IPv6 常被分到内网段（如 fdfe:dcba:9876::/64）；只要 IPv4 是 fake-ip，就丢掉这些内网 IPv6 再判断。
const lookupFrom = "  if (!addresses.length) throw new Error('域名没有解析结果');\n";
if (!safe.includes(lookupFrom)) throw new Error('找不到替换点：fake-ip lookup');
safe = safe.replace(lookupFrom, lookupFrom +
  "  if (FAKE_IP_OK && addresses.some((item) => isFakeIpV4(item.address))) {\n" +
  "    addresses = addresses.filter((item) => Number(item?.family) === 4 || !isPrivateIp(item.address));\n" +
  "  }\n");
const fakeHelper = "function isFakeIpV4(ip) {\n  const parts = String(ip || '').split('.').map(Number);\n  return net.isIP(String(ip || '')) === 4 && parts[0] === 198 && parts[1] >= 18 && parts[1] <= 19;\n}\n\n";
safe = safe.replace(safeAnchor, fakeHelper + "const FAKE_IP_OK = process.env.FISH_ALLOW_FAKE_IP === '1';\n\n" + safeAnchor);
fs.writeFileSync(path.join(out, 'safe-fetch.js'), '// 本文件来自「鱼鱼找鸡 v1.0.0」（MIT，© 2026 小鲸鱼），仅增加 FISH_ALLOW_FAKE_IP 开关。\n' + safe);
console.log(`ok：${code.split('\n').length} 行 → ${out}`);
