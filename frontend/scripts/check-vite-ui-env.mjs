/**
 * 构建前检查：避免未读取 .env.local 时打出「未注入登录密码」的生产包。
 * 若设置 VITE_UI_AUTH_DISABLED=true 则跳过。
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.resolve(__dirname, '..');

function parseEnvText(text) {
  /** @type {Record<string, string>} */
  const out = {};
  for (const line of text.split(/\r?\n/)) {
    const t = line.trim();
    if (!t || t.startsWith('#')) continue;
    const eq = t.indexOf('=');
    if (eq === -1) continue;
    const key = t.slice(0, eq).trim();
    let val = t.slice(eq + 1).trim();
    if (
      (val.startsWith('"') && val.endsWith('"')) ||
      (val.startsWith("'") && val.endsWith("'"))
    ) {
      val = val.slice(1, -1);
    }
    out[key] = val;
  }
  return out;
}

function loadMergedEnvFiles() {
  /** @type {Record<string, string>} */
  let merged = {};
  const names = ['.env', '.env.local', '.env.production', '.env.production.local'];
  for (const name of names) {
    const p = path.join(frontendDir, name);
    if (!fs.existsSync(p)) continue;
    try {
      const text = fs.readFileSync(p, 'utf8');
      merged = { ...merged, ...parseEnvText(text) };
    } catch {
      /* ignore */
    }
  }
  return merged;
}

const env = loadMergedEnvFiles();

if (env.VITE_UI_AUTH_DISABLED === 'true') {
  console.log('[check-vite-ui-env] VITE_UI_AUTH_DISABLED=true，跳过密码检查');
  process.exit(0);
}

const owner = (env.VITE_UI_OWNER_PASSWORD ?? '').trim();
const guest = (env.VITE_UI_GUEST_PASSWORD ?? '').trim();

if (owner === '' && guest === '') {
  console.error(
    '\n[check-vite-ui-env] 未在 frontend 目录找到有效的 VITE_UI_OWNER_PASSWORD / VITE_UI_GUEST_PASSWORD。\n' +
      '请在 frontend/.env.local 中填写（不要用 .env.example 代替），保存后重新执行 npm run build。\n' +
      '确认文件路径为：' +
      path.join(frontendDir, '.env.local') +
      '\n',
  );
  process.exit(1);
}

console.log('[check-vite-ui-env] 已检测到 UI 登录密码配置，继续构建');
