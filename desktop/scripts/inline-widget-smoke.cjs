// Isolated real-Electron validation; no Gateway, API credentials or model calls.
const path=require('node:path');
const esbuild=require('esbuild');
const fs=require('node:fs');
const root=path.resolve(__dirname,'../..');const out=root+'/output/playwright/inline-widget';
fs.mkdirSync(out,{recursive:true});
const profile=fs.mkdtempSync(require('node:os').tmpdir()+'/ace-inline-smoke-');
fs.writeFileSync(out+'/renderer.ts', `
import {state} from '${root}/desktop/src/ui/state';
import {bindInlineWidgets,renderInlineWidget,hydrateInlineWidgets} from '${root}/desktop/src/ui/features/inline-widget';
state.isLoggedIn=true;state.userInfo={uid:'smoke-owner'};state.activeSessionId='smoke-session';
window.sent=[];bindInlineWidgets(async(s,t)=>{window.sent.push({s,t});return true});
const widget={key:'smoke',sessionId:'smoke-session',widgetId:'widget_123456789abc',title:'收益情景分析',resourceRevision:1,html:${JSON.stringify(fs.readFileSync(root+'/crew/skills/widget/examples/inline-revenue.html','utf8'))}};
document.body.append(renderInlineWidget(widget));hydrateInlineWidgets(document.body);
`);
fs.writeFileSync(out+'/main.ts', `
import {app,BrowserWindow,protocol} from 'electron';
import {registerSitePreviewProtocol} from '${root}/desktop/src/main/site-preview-protocol';
app.setPath('userData',${JSON.stringify(profile)});
protocol.registerSchemesAsPrivileged([{scheme:'ace-site',privileges:{standard:true,secure:true,supportFetchAPI:true}}]);
app.whenReady().then(()=>{registerSitePreviewProtocol(async()=>{throw Error('unexpected gateway fetch')});const win=new BrowserWindow({width:1000,height:950,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false}});win.loadFile('${out}/index.html')});
`);
const csp=fs.readFileSync(root+'/desktop/assets/index.html','utf8').match(/<meta http-equiv="Content-Security-Policy"[^>]+>/)[0];
fs.writeFileSync(out+'/index.html',`<!doctype html><html><head>${csp}<style>body{background:#191b22;color:#ddd;max-width:850px;margin:30px auto;font:14px system-ui;--mw-space-2:8px;--mw-space-3:12px;--mw-space-4:16px;--mw-border-subtle:#454a56;--mw-radius-2:6px;--mw-radius-4:12px;--mw-text-primary:#eee;--mw-text-secondary:#aaa;--mw-type-caption-size:13px}${fs.readFileSync(root+'/desktop/assets/styles/web-messages.css','utf8')}</style></head><body><h1>桌面回答交互验证</h1><script src="renderer.js"></script></body></html>`);
async function build(){await esbuild.build({entryPoints:[out+'/renderer.ts'],outfile:out+'/renderer.js',bundle:true,platform:'browser'});await esbuild.build({entryPoints:[out+'/main.ts'],outfile:out+'/main.cjs',bundle:true,platform:'node',external:['electron']})}

const {_electron}=require('playwright-core');
const assert=require('node:assert/strict');
(async()=>{
 await build();
 const app=await _electron.launch({executablePath:require('electron'),args:[out+'/main.cjs']});
 try{
 const page=await app.firstWindow();page.on('console',m=>console.log('browser',m.type(),m.text()));
 const f=page.frameLocator('iframe');
 await f.locator('#income').waitFor({timeout:15000});
 await f.locator('#income').fill('12000');
 await f.locator('#cost').fill('9000');
 await f.locator('#initial').fill('5000');
 assert.match(await f.locator('#costLabel').innerText(),/9,000/);
 assert.equal(await f.locator('polyline').count(),3);
 const expected=await f.locator('#cards').innerText();
 await page.screenshot({path:out+'/desktop-interactive.png',fullPage:true});
 await page.getByRole('button',{name:'用当前参数继续分析'}).click();
 await page.getByText('参数已提交，可修改后再次分析').waitFor();
 assert.equal(await page.evaluate(()=>window.sent.length),1);
 assert.match(await page.evaluate(()=>window.sent[0].t),/"cost":9000/);
 assert.equal(await page.getByRole('button',{name:'用当前参数继续分析'}).isDisabled(),true);
 await page.reload();
 await f.locator('#income').waitFor();
 assert.equal(await f.locator('#income').inputValue(),'12000');
 assert.equal(await f.locator('#cost').inputValue(),'9000');
 assert.equal(await f.locator('#cards').innerText(),expected);
 const frame=page.frames().find(x=>x.url().startsWith('ace-site:'));
 assert.equal(await frame.evaluate(()=>{try{return !!parent.document}catch{return false}}),false);
 assert.equal(await frame.evaluate(()=>typeof window.Crew),'undefined');
 assert.equal(await frame.evaluate(async()=>{try{await fetch('https://example.com');return true}catch{return false}}),false);
 await f.locator('#income').fill('-1');
 await page.getByRole('button',{name:'用当前参数继续分析'}).click();
 await page.getByText('请检查输入，并在所属聊天中提交').waitFor();
 assert.equal(await page.evaluate(()=>window.sent.length),0);
 console.log('PASS: chart recalculation, input persistence, submit once, validation, CSP, opaque origin');
 }finally{await app.close();fs.rmSync(profile,{recursive:true,force:true});for(const file of ['renderer.ts','renderer.js','main.ts','main.cjs','index.html'])fs.rmSync(out+'/'+file,{force:true})}
})().catch(e=>{console.error(e);process.exit(1)});
