/** Render checked-in Mermaid sources; no model, platform or project service calls.
 * node docs/render_project_diagrams.mjs --deps-root PATH_TO_NODE_MODULES
 * Optional: --playwright-root PATH_TO_PLAYWRIGHT_PACKAGE
 */
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import fs from 'node:fs/promises';
import http from 'node:http';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const option = (name) => args.includes(name) ? args[args.indexOf(name) + 1] : undefined;
const deps = path.resolve(option('--deps-root') || path.join(repo, '.tmp/diagram-tools/node_modules'));
const resolve = createRequire(path.join(deps, '_diagram_resolver.cjs'));
const { chromium } = option('--playwright-root')
  ? createRequire(import.meta.url)(path.resolve(option('--playwright-root')))
  : resolve('playwright');
const mermaidDist = path.join(path.dirname(resolve.resolve('mermaid/package.json')), 'dist');
const output = path.join(repo, 'docs/diagrams');
const mainSource = path.join(repo, 'docs/知识图谱.md');
const names = ['architecture', 'content_publish', 'knowledge_ingest', 'knowledge_search', 'knowledge_recovery', 'entity_graph'];
const inputs = [mainSource, path.join(repo, 'README.md'), path.join(repo, 'docs/PROJECT_TECHNICAL_WHITEPAPER.md')];

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (url.pathname === '/') {
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end('<!doctype html><meta charset="utf-8"><style>body{margin:0;background:white}#diagram{display:inline-block;padding:24px}</style><div id="diagram"></div>');
      return;
    }
    const rel = decodeURIComponent(url.pathname).replace(/^\/+/, '');
    const target = path.resolve(mermaidDist, rel);
    if (!target.startsWith(mermaidDist + path.sep)) throw new Error('outside dependency dist');
    const data = await fs.readFile(target);
    res.setHeader('Content-Type', target.endsWith('.js') || target.endsWith('.mjs') ? 'text/javascript' : 'application/octet-stream');
    res.end(data);
  } catch {
    res.writeHead(404).end();
  }
});
let browser;
try {
  await new Promise((done) => server.listen(0, '127.0.0.1', done));
  const address = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 2000, height: 1200 }, deviceScaleFactor: 1.5 });
  await page.goto(address);
  await page.evaluate(async () => {
    window.mermaid = (await import('/mermaid.esm.min.mjs')).default;
    window.mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'base',
      fontFamily: 'Microsoft YaHei, Arial, sans-serif',
      themeVariables: { primaryColor: '#EDF4FC', primaryTextColor: '#172A46', primaryBorderColor: '#7294BC',
        lineColor: '#728399', clusterBkg: '#F7F9FC', clusterBorder: '#CDD8E5', fontSize: '17px' },
      flowchart: { htmlLabels: true, useMaxWidth: false, curve: 'linear', nodeSpacing: 28, rankSpacing: 42 } });
  });
  await fs.mkdir(output, { recursive: true });
  const checks = [];
  let serial = 0;
  for (const input of inputs) {
    const text = await fs.readFile(input, 'utf8');
    const blocks = [...text.matchAll(/```mermaid\s*\r?\n([\s\S]*?)```/g)].map((m) => m[1]);
    if (input === mainSource && blocks.length !== names.length) throw new Error('Update diagram names when source diagram count changes');
    for (const [index, source] of blocks.entries()) {
      const result = await page.evaluate(async ({ source, serial }) => {
        await window.mermaid.parse(source);
        const result = await window.mermaid.render('diagram_' + serial, source);
        document.getElementById('diagram').innerHTML = result.svg;
        await document.fonts.ready;
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const node = document.querySelector('#diagram svg');
        const viewBox = node.viewBox.baseVal;
        if (viewBox.width && viewBox.height) {
          node.style.maxWidth = 'none';
          node.style.width = viewBox.width + 'px';
          node.style.height = viewBox.height + 'px';
        }
        const box = node.getBoundingClientRect();
        const badLabels = [...node.querySelectorAll('foreignObject')].filter((n) => n.textContent.trim() && n.getBoundingClientRect().width < 1).length;
        return { svg: node.outerHTML, width: Math.ceil(box.width), height: Math.ceil(box.height), badLabels };
      }, { source, serial: ++serial });
      if (!result.width || !result.height || result.badLabels) throw new Error(`Invalid diagram ${path.basename(input)} #${index + 1}: ${JSON.stringify({ width: result.width, height: result.height, badLabels: result.badLabels })}`);
      if (input === mainSource) {
        const basename = path.join(output, names[index]);
        await fs.writeFile(basename + '.svg', result.svg);
        await fs.writeFile(basename + '.mmd', source);
        await page.locator('#diagram').screenshot({ path: basename + '.png', timeout: 30000 });
      }
      checks.push({ source: path.relative(repo, input), diagram: index + 1, width: result.width, height: result.height });
    }
  }
  console.log(JSON.stringify({ rendered_and_parsed: checks.length, exported_images: names.length, checks }, null, 2));
} finally {
  if (browser) await browser.close();
  await new Promise((done) => server.close(done));
}
