package httpapi

const fallbackIndexHTML = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>YYB Go 控制台</title>
<body style="margin:0;background:oklch(0.974 0.004 250);color:oklch(0.19 0.025 252);font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif">
<main style="max-width:960px;margin:48px auto;padding:0 24px">
<section style="background:oklch(1 0 0);border:1px solid oklch(0.885 0.012 250);border-radius:8px;padding:24px">
<h1 style="margin:0 0 8px;font-size:24px">YYB Go 控制台</h1>
<p style="margin:0 0 20px;color:oklch(0.43 0.025 252)">资源模板未找到，服务仍可通过以下入口使用。</p>
<p style="display:flex;gap:10px;flex-wrap:wrap;margin:0">
<a style="padding:10px 12px;border-radius:8px;background:oklch(0.54 0.205 3);color:oklch(1 0 0);text-decoration:none" href="/scan">扫码添加</a>
<a style="padding:10px 12px;border-radius:8px;border:1px solid oklch(0.885 0.012 250);color:inherit;text-decoration:none" href="/docs/index.html">Swagger 文档</a>
<a style="padding:10px 12px;border-radius:8px;border:1px solid oklch(0.885 0.012 250);color:inherit;text-decoration:none" href="/openapi.json">OpenAPI JSON</a>
</p>
</section>
</main>
</body></html>`

const fallbackScanHTML = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>扫码添加账号</title>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;background:oklch(0.974 0.004 250);color:oklch(0.19 0.025 252);font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif">
<main style="width:min(420px,calc(100vw - 32px));background:oklch(1 0 0);border:1px solid oklch(0.885 0.012 250);border-radius:8px;padding:24px;text-align:center">
<h1 style="margin:0 0 8px;font-size:22px">扫码添加账号</h1>
<p id="s" style="margin:0 0 18px;color:oklch(0.43 0.025 252)">正在生成二维码</p>
<div id="qr" style="width:240px;height:240px;margin:0 auto 18px;display:grid;place-items:center;border:1px solid oklch(0.885 0.012 250);border-radius:8px;background:oklch(0.986 0.004 250)">请稍候</div>
<p style="display:flex;gap:10px;justify-content:center;margin:0">
<button onclick="newQR()" style="border:0;border-radius:8px;padding:10px 12px;background:oklch(0.54 0.205 3);color:oklch(1 0 0)">重新生成</button>
<a href="/" style="border:1px solid oklch(0.885 0.012 250);border-radius:8px;padding:9px 12px;color:inherit;text-decoration:none">返回首页</a>
</p>
</main>
<script>
let sid,timer;
async function api(url,options){
 const resp=await fetch(url,options);
 const text=await resp.text();
 let data=null;
 try{data=text?JSON.parse(text):null}catch(e){data=text}
 const isEnvelope=data&&typeof data==='object'&&!Array.isArray(data)&&Object.prototype.hasOwnProperty.call(data,'code')&&Object.prototype.hasOwnProperty.call(data,'msg')&&Object.prototype.hasOwnProperty.call(data,'data');
 if(!resp.ok||(isEnvelope&&data.code!==0)){throw new Error(isEnvelope?data.msg:'HTTP '+resp.status)}
 return isEnvelope?data.data:data;
}
async function newQR(){
 clearInterval(timer);
 document.getElementById('qr').textContent='请稍候';
 document.getElementById('s').textContent='正在生成二维码';
 const r=await api('/qr?as_base64=true',{method:'POST'});
 sid=r.session_id;
 const source=r.image_base64||r.image_url;
 document.getElementById('qr').innerHTML='<img alt="二维码" style="width:240px;height:240px" src="'+source+'" onerror="this.parentElement.textContent=\'二维码图片加载失败，请检查反向代理是否转发 /qr/* 路径\'">';
 document.getElementById('s').textContent='等待扫码';
 timer=setInterval(poll,1500);
}
async function poll(){
 const r=await api('/qr/'+sid+'/poll');
 document.getElementById('s').textContent=r.status;
 if(r.status==='authorized'){
  clearInterval(timer);
  const a=await api('/qr/'+sid+'/confirm',{method:'POST'});
  document.getElementById('s').textContent='添加成功: '+(a.nickname||a.openid);
 }
 if(['expired','cancelled','unknown'].includes(r.status)){clearInterval(timer)}
}
newQR();
</script></body></html>`

const fallbackProxiesHTML = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>代理设置 - YYB Go</title><body style="font-family:system-ui,sans-serif;margin:40px"><main><h1>代理设置</h1>
<p>资源模板未找到，请检查容器内的 <code>/app/resource/templates/proxies.html</code>。</p><p><a href="/">返回工作台</a></p></main></body></html>`

const fallbackRunsHTML = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>账号运行管理</title><body style="font-family:system-ui,sans-serif;margin:40px"><h1>账号运行管理</h1>
<p>资源模板未找到，请检查容器内的 <code>/app/resource/templates/runs.html</code>。</p><p><a href="/">返回账号控制台</a></p></body></html>`

const docsHTML = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>YYB Go API 文档</title>
<body style="margin:32px;font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;line-height:1.6">
<h1>YYB Go API</h1>
<p>Swagger UI: <a href="/docs/index.html">/docs/index.html</a></p>
<p>OpenAPI JSON: <a href="/openapi.json">/openapi.json</a></p>
</body></html>`

var openAPISpec = newOpenAPISpec()

const fallbackLoginHTML = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>登录 - YYB Go</title><body><main><h1>YYB Go 登录</h1><p>资源模板未找到，请检查 <code>resource/templates/login.html</code>。</p></main></body></html>`
const fallbackRegisterHTML = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>注册 - YYB Go</title><body><main><h1>YYB Go 注册</h1><p>资源模板未找到，请检查 <code>resource/templates/register.html</code>。</p></main></body></html>`
const fallbackSettingsHTML = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>个人设置 - YYB Go</title><body><main><h1>个人设置</h1><p>资源模板未找到。</p></main></body></html>`
const fallbackUsersHTML = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>用户管理 - YYB Go</title><body><main><h1>用户管理</h1><p>资源模板未找到。</p></main></body></html>`
const fallbackAccountLinksHTML = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>已激活短链接 - YYB Go</title><body><main><h1>已激活短链接</h1><p>资源模板未找到，请检查 <code>resource/templates/account-links.html</code>。</p><p><a href="/">返回工作台</a></p></main></body></html>`
