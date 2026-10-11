// Builds the static page into public/: fingerprints the script and stylesheet so
// they can be cached for a year, and gives the service worker its file list.
import { createHash } from 'node:crypto';
import { cpSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = new URL('./ui/', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
const OUT = new URL('./public/', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
const FINGERPRINTED = ['app.css', 'app.js', 'theme-boot.js'];

const hash = (buf) => createHash('sha256').update(buf).digest('hex').slice(0, 10);

mkdirSync(OUT, { recursive: true });
for (const name of readdirSync(OUT)) rmSync(new URL(name, new URL('./public/', import.meta.url)), { recursive: true, force: true });

function walk(dir, base = '') {
  return readdirSync(dir).flatMap((name) => {
    const rel = base ? `${base}/${name}` : name;
    return statSync(join(dir, name)).isDirectory() ? walk(join(dir, name), rel) : [rel];
  });
}
const files = walk(SRC);

const renamed = new Map();
for (const name of FINGERPRINTED) {
  const buf = readFileSync(join(SRC, name));
  const dot = name.lastIndexOf('.');
  renamed.set(name, `${name.slice(0, dot)}.${hash(buf)}${name.slice(dot)}`);
}

let html = readFileSync(join(SRC, 'index.html'), 'utf8');
for (const [from, to] of renamed) html = html.replaceAll(`"/${from}"`, `"/${to}"`);
// A phone fetches linked home-screen icons in a separate process, so the page also carries them inline.
const dataUri = (rel, type) => `data:${type};base64,${readFileSync(join(SRC, rel)).toString('base64')}`;
html = html.replace('href="/icons/apple-touch-icon.png"', `href="${dataUri('icons/apple-touch-icon.jpg', 'image/jpeg')}"`);
html = html.replace('href="/favicon.png"', `href="${dataUri('favicon.png', 'image/png')}"`);
writeFileSync(join(OUT, 'index.html'), html);

const shell = ['/'];
const versionParts = [html];
for (const rel of files) {
  if (rel === 'index.html' || rel === 'sw.js' || rel === 'manifest.webmanifest' || rel.startsWith('fonts/LICENSE') || rel.endsWith('.jpg')) {
    if (rel === 'manifest.webmanifest') cpSync(join(SRC, rel), join(OUT, rel));
    if (rel.startsWith('fonts/LICENSE')) { mkdirSync(join(OUT, 'fonts'), { recursive: true }); cpSync(join(SRC, rel), join(OUT, rel)); }
    continue;
  }
  const outName = renamed.get(rel) ?? rel;
  mkdirSync(join(OUT, outName, '..'), { recursive: true });
  cpSync(join(SRC, rel), join(OUT, outName));
  // The large icons are for the home screen and the install prompt, not the page.
  if (!/^icons\/(icon-512|icon-maskable|apple-touch)/.test(rel)) shell.push(`/${outName}`);
  versionParts.push(outName);
}
// iOS also asks for these two root paths directly, so they must be real images.
for (const name of ['apple-touch-icon.png', 'apple-touch-icon-precomposed.png']) cpSync(join(SRC, 'icons/apple-touch-icon.png'), join(OUT, name));
cpSync(join(SRC, 'favicon.png'), join(OUT, 'favicon.ico'));
shell.sort();

let sw = readFileSync(join(SRC, 'sw.js'), 'utf8');
sw = sw.replace('__VERSION__', hash(versionParts.join('\n'))).replace('__SHELL__', JSON.stringify(shell, null, 2));
writeFileSync(join(OUT, 'sw.js'), sw);

console.log(`built ${shell.length} shell files into public/`);
