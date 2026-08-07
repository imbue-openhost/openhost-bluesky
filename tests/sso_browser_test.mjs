import { chromium } from '/usr/lib/node_modules/playwright/index.mjs';
const APP='andrew', ZONE='bsky-sso.selfhost.imbue.com', COOKIE=process.argv[2];
const BASE=`https://${APP}.${ZONE}`, HANDLE=`${APP}.${ZONE}`;
let pass=0,fail=0; const ok=(n,c,d='')=>{c?(pass++,console.log(`PASS ${pass+fail}: ${n}`)):(fail++,console.log(`FAIL ${pass+fail}: ${n} [${d}]`));};
const b=await chromium.launch({args:['--ignore-certificate-errors']});
const ctx=await b.newContext({ignoreHTTPSErrors:true});
// OpenHost owner session cookie -> router stamps X-OpenHost-Is-Owner: true
await ctx.addCookies([{name:'session_token',value:COOKIE,domain:`${APP}.${ZONE}`,path:'/',httpOnly:true,secure:true}]);
const p=await ctx.newPage();
let sawBootstrap=false, createSessionStatus=null;
p.on('response',r=>{if(r.url().includes('com.atproto.server.createSession'))createSessionStatus=r.status();});
// First navigation: expect SSO bootstrap then auto-redirect into logged-in app
await p.goto(BASE+'/',{waitUntil:'domcontentloaded',timeout:45000});
// bootstrap page contains "Signing you in"; may flash quickly then reload
await p.waitForTimeout(9000);
const state=await p.evaluate(()=>{
  let stored=null; try{stored=JSON.parse(localStorage.getItem('BSKY_STORAGE'))?.session?.currentAccount?.handle;}catch(e){}
  return {
    text:(document.body.innerText||'').slice(0,300),
    hasLoginForm:/username and password|Create account/i.test(document.body.innerText||''),
    hasLoginDialog:/username and password|Create account/i.test(document.querySelector('[role="dialog"]')?.innerText||''),
    storedHandle:stored,
    hasCookie:document.cookie.includes('oh_sso'),
  };
});
ok('SSO seeded session into client store', state.storedHandle===HANDLE, 'stored='+state.storedHandle);
ok('oh_sso cookie set (no re-seed loop)', state.hasCookie, 'cookie='+state.hasCookie);
ok('owner NOT shown a login prompt (seamless)', !state.hasLoginDialog, state.text.replace(/\n/g,' ').slice(0,100));
ok('app rendered content', state.text.length>20, 'len='+state.text.length);
// reload: should stay logged in, and NOT re-trigger bootstrap (cookie present)
await p.reload({waitUntil:'domcontentloaded'}); await p.waitForTimeout(6000);
const after=await p.evaluate(()=>({stillLoggedIn:!/username and password|^Create account/i.test(document.querySelector('[role="dialog"]')?.innerText||''),handle:(()=>{try{return JSON.parse(localStorage.getItem('BSKY_STORAGE'))?.session?.currentAccount?.handle;}catch(e){return null;}})()}));
ok('still logged in after reload', after.stillLoggedIn && after.handle===HANDLE, 'handle='+after.handle);
console.log('='.repeat(45));console.log(`SSO BROWSER: PASS=${pass} FAIL=${fail}`);
await b.close(); process.exit(fail?1:0);
