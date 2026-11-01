#!/usr/bin/env node
// AstrBot 插件专用入口：只接收 Python 侧已经下载并校验过的临时图片文件。
// 不接受网址、不读 config.json、不读任何其它本机文件。结果以一行 JSON 写到 stdout。
import fs from 'node:fs';
import path from 'node:path';
import {
  reverseSearch,
  extractAccounts,
  formatReply,
  closeBrowser,
  ENGINE_IDS
} from './fish-find-chicken.js';

const MAX_BYTES = 20 * 1024 * 1024;

function parseArgs(argv) {
  const out = { image: '', config: {} };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--image') out.image = String(argv[++i] ?? '');
    else if (a === '--config-json') out.config = JSON.parse(String(argv[++i] ?? '{}'));
    else if (a === '--config-b64') out.config = JSON.parse(Buffer.from(String(argv[++i] ?? ''), 'base64').toString('utf8') || '{}');
    else throw new Error(`不认识的参数：${a}`);
  }
  if (!out.image) throw new Error('缺少 --image');
  return out;
}

function sniffMime(bytes) {
  if (bytes.length > 8 && bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47) return 'image/png';
  if (bytes.length > 12 && bytes.subarray(0, 4).toString('ascii') === 'RIFF' && bytes.subarray(8, 12).toString('ascii') === 'WEBP') return 'image/webp';
  if (bytes.length > 3 && bytes[0] === 0x47 && bytes[1] === 0x49 && bytes[2] === 0x46) return 'image/gif';
  if (bytes.length > 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return 'image/jpeg';
  return '';
}

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const cfg = args.config && typeof args.config === 'object' ? args.config : {};

  // 只允许读插件数据目录 tmp 里的文件，避免这个入口被当成「任意文件读取器」
  const stateDir = process.env.FISH_STATE_DIR ? path.resolve(process.env.FISH_STATE_DIR) : '';
  if (!stateDir) throw new Error('缺少 FISH_STATE_DIR');
  const tempDir = path.join(stateDir, 'tmp');
  const file = fs.realpathSync(path.resolve(args.image));
  const realTemp = fs.realpathSync(tempDir);
  if (path.dirname(file) !== realTemp) throw new Error('图片路径不在插件临时目录里');

  const stat = fs.statSync(file);
  if (!stat.isFile()) throw new Error('不是文件');
  if (stat.size > MAX_BYTES) throw new Error('图片超过 20MB');
  const bytes = fs.readFileSync(file);
  const mimeType = sniffMime(bytes);
  if (!mimeType) throw new Error('不是 jpg/png/webp/gif 图片');

  const engines = (Array.isArray(cfg.engines) ? cfg.engines : ['lens', 'saucenao', 'tracemoe'])
    .map((s) => String(s).trim())
    .filter((id) => ENGINE_IDS.includes(id));
  if (!engines.length) throw new Error('没有可用的识图引擎（检查 engines 配置）');

  const started = Date.now();
  const logs = [];
  const result = await reverseSearch(bytes, mimeType, {
    engines,
    timeoutMs: Math.max(5000, Number(cfg.timeoutMs) || 45000),
    proxy: String(cfg.proxy || ''),
    browserExe: String(cfg.browserExe || ''),
    tempDir,
    onLog: (line) => { if (logs.length < 50) logs.push(String(line)); }
  });
  const accounts = extractAccounts(result.candidates, {
    max: Math.max(1, Math.min(10, Number(cfg.maxAccounts) || 3)),
    strictNames: cfg.strictConsensus !== false
  });
  const text = formatReply({
    accounts,
    candidates: result.candidates,
    enginesUsed: result.enginesUsed,
    failures: result.failures,
    cached: false
  });
  emit({
    ok: true,
    text,
    accounts,
    enginesUsed: result.enginesUsed,
    failures: result.failures,
    elapsedMs: Date.now() - started,
    logs
  });
}

// 自己的总超时 + 收到 SIGTERM/SIGINT 时也先关浏览器再退出：
// Linux 下浏览器自成进程组，父进程被硬杀会留下孤儿浏览器。
let finishing = false;
async function bail(message) {
  if (finishing) return;
  finishing = true;
  emit({ ok: false, error: message });
  try { await closeBrowser(); } catch {}
  process.exit(1);
}
const totalSec = Number(process.env.FISH_TOTAL_TIMEOUT_SEC) || 0;
if (totalSec > 0) setTimeout(() => { bail(`总耗时超过 ${totalSec} 秒，已停止`); }, totalSec * 1000);
for (const sig of ['SIGTERM', 'SIGINT']) process.on(sig, () => { bail(`收到 ${sig}，已停止`); });

let code = 0;
try {
  await main();
} catch (error) {
  emit({ ok: false, error: String(error?.message ?? error) });
  code = 1;
} finally {
  finishing = true;
  try { await closeBrowser(); } catch {}
}
process.exit(code);