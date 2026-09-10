from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, status
from mcp.server.fastmcp.utilities.types import Image
from websockets.sync.client import connect


def _debug_port() -> int:
    try:
        return int(os.getenv("CLOUD_MCP_CHROMIUM_DEBUG_PORT", "9222"))
    except ValueError:
        return 9222


def _base_url() -> str:
    return f"http://127.0.0.1:{_debug_port()}"


def _json_get(path: str) -> Any:
    try:
        response = httpx.get(_base_url() + path, timeout=5.0)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Chromium DevTools endpoint is unavailable on localhost:{_debug_port()}: {exc}",
        ) from exc


def browser_list_tabs() -> Dict[str, Any]:
    targets = _json_get("/json/list")
    tabs = []
    for item in targets:
        if item.get("type") != "page":
            continue
        url = str(item.get("url") or "")
        if url.startswith("devtools://"):
            continue
        tabs.append({
            "tab_id": item.get("id"),
            "title": item.get("title") or "",
            "url": url,
        })
    return {"ok": True, "debug_port": _debug_port(), "count": len(tabs), "tabs": tabs}


def _target(tab_id: Optional[str] = None) -> Dict[str, Any]:
    targets = _json_get("/json/list")
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if tab_id:
        for target in pages:
            if str(target.get("id")) == str(tab_id):
                return target
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Chromium tab_id not found: {tab_id}")
    if not pages:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No inspectable Chromium page tab is open")
    # Chrome's /json/list is normally most-recent/active first. Prefer ordinary web pages.
    ordinary = [p for p in pages if str(p.get("url") or "").startswith(("http://", "https://", "file://", "about:"))]
    return (ordinary or pages)[0]


def _cdp_call(target: Dict[str, Any], method: str, params: Optional[Dict[str, Any]] = None,
              timeout_s: float = 12.0) -> Dict[str, Any]:
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Chromium target has no DevTools websocket URL")
    message_id = int(time.time_ns() % 2_000_000_000)
    try:
        with connect(ws_url, open_timeout=5, close_timeout=2) as ws:
            ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                raw = ws.recv(timeout=max(0.1, deadline - time.monotonic()))
                payload = json.loads(raw)
                if payload.get("id") != message_id:
                    continue
                if "error" in payload:
                    raise RuntimeError(payload["error"].get("message") or str(payload["error"]))
                return payload.get("result") or {}
            raise TimeoutError(f"Timed out waiting for CDP method {method}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"CDP {method} failed: {exc}") from exc




def _browser_target() -> Dict[str, Any]:
    info = _json_get("/json/version")
    ws_url = info.get("webSocketDebuggerUrl")
    if not ws_url:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Chromium browser DevTools websocket URL is unavailable")
    return {"webSocketDebuggerUrl": ws_url}


def browser_activate_tab(tab_id: str) -> Dict[str, Any]:
    _cdp_call(_browser_target(), "Target.activateTarget", {"targetId": str(tab_id)}, timeout_s=8)
    return {"ok": True, "tab_id": str(tab_id), "active": True}


def browser_close_tab(tab_id: str) -> Dict[str, Any]:
    result = _cdp_call(_browser_target(), "Target.closeTarget", {"targetId": str(tab_id)}, timeout_s=8)
    success = bool(result.get("success", True))
    return {"ok": success, "tab_id": str(tab_id), "closed": success}


def _evaluate(target: Dict[str, Any], expression: str, timeout_s: float = 15.0) -> Any:
    result = _cdp_call(target, "Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": True,
        "userGesture": True,
    }, timeout_s=timeout_s)
    remote = result.get("result") or {}
    if remote.get("subtype") == "error":
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, remote.get("description") or "Runtime.evaluate failed")
    if "value" not in remote:
        exception = result.get("exceptionDetails") or {}
        if exception:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exception.get("text") or exception))
        return None
    return remote.get("value")


_BOOTSTRAP = r'''
function __mcpState(){
  var s=window.__cloudMcpBrowserAgent;
  if(!s){
    s=window.__cloudMcpBrowserAgent={counter:0,ids:new WeakMap(),elements:Object.create(null),pageToken:Math.random().toString(36).slice(2,10),mutationRevision:0,observations:Object.create(null),observer:null};
    try{s.observer=new MutationObserver(function(){s.mutationRevision+=1;});s.observer.observe(document.documentElement||document,{subtree:true,childList:true,attributes:true,characterData:true});}catch(e){}
  }
  return s;
}
function __mcpId(el,s){var id=s.ids.get(el);if(!id){id='e'+(++s.counter);s.ids.set(el,id);}s.elements[id]=el;return id;}
function __mcpVisible(el){if(!el||el.nodeType!==1)return false;var st=getComputedStyle(el);if(st.display==='none'||st.visibility==='hidden'||parseFloat(st.opacity||'1')===0)return false;var r=el.getBoundingClientRect();if(r.width<2||r.height<2)return false;return r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth;}
function __mcpActionable(el){var tag=(el.tagName||'').toLowerCase(),role=(el.getAttribute('role')||'').toLowerCase();if(['a','button','input','textarea','select','summary','details'].includes(tag))return true;if(['button','link','checkbox','radio','tab','menuitem','option','combobox','textbox','searchbox','switch','slider'].includes(role))return true;if(el.getAttribute('contenteditable')==='true'||el.hasAttribute('onclick'))return true;var ti=el.getAttribute('tabindex');if(ti!==null&&Number(ti)>=0)return true;try{var cur=getComputedStyle(el).cursor;if(cur==='pointer'&&!['svg','use','path'].includes(tag)){var p=el.parentElement,pc=p?getComputedStyle(p).cursor:'';if(pc!=='pointer')return true;}}catch(e){}return false;}
function __mcpText(el){var aria=el.getAttribute('aria-label')||'',ph=el.getAttribute('placeholder')||'',title=el.getAttribute('title')||'',txt='';try{txt=(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim();}catch(e){}return (aria||ph||title||txt).slice(0,240);}
function __mcpRole(el){var role=el.getAttribute('role');if(role)return role;var tag=(el.tagName||'').toLowerCase();if(tag==='a')return 'link';if(tag==='button')return 'button';if(tag==='select')return 'combobox';if(tag==='textarea'||el.getAttribute('contenteditable')==='true')return 'textbox';if(tag==='input'){var t=(el.type||'text').toLowerCase();if(t==='checkbox')return 'checkbox';if(t==='radio')return 'radio';if(['button','submit','reset'].includes(t))return 'button';return 'textbox';}return '';}
function __mcpRect(el){var r=el.getBoundingClientRect();return {viewport:{x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),h:Math.round(r.height)},document:{x:Math.round(r.left+scrollX),y:Math.round(r.top+scrollY),w:Math.round(r.width),h:Math.round(r.height)}};}
function __mcpDescribe(el,s){var tag=(el.tagName||'').toLowerCase(),rect=__mcpRect(el);var out={element_id:__mcpId(el,s),tag:tag,role:__mcpRole(el),text:__mcpText(el),viewport_rect:rect.viewport,document_rect:rect.document,actionable:__mcpActionable(el)};var aria=el.getAttribute('aria-label')||'',ph=el.getAttribute('placeholder')||'',name=el.getAttribute('name')||'',title=el.getAttribute('title')||'';if(aria)out.aria_label=aria.slice(0,120);if(ph)out.placeholder=ph.slice(0,120);if(name)out.name=name.slice(0,100);if(title)out.title=title.slice(0,120);if(tag==='a'&&el.href)out.href=String(el.href).slice(0,240);if(el.disabled===true)out.enabled=false;else out.enabled=true;if(document.activeElement===el)out.focused=true;if(['input','textarea','select'].includes(tag))out.value=String(el.value||'').slice(0,200);if(el.getAttribute('contenteditable')==='true'){out.contenteditable=true;out.value=String(el.innerText||el.textContent||'').slice(0,200);}if(tag==='input'&&el.type)out.input_type=String(el.type);if(typeof el.checked==='boolean')out.checked=!!el.checked;if(tag==='select')out.options=Array.from(el.options||[]).slice(0,30).map(function(o){return {text:String(o.text||'').slice(0,100),value:String(o.value||'').slice(0,100),selected:!!o.selected};});return out;}
'''


def _observe_js(scope: str, max_elements: int) -> str:
    scope_json = json.dumps(scope)
    return f'''(function(){{
{_BOOTSTRAP}
function ownText(el){{var out=[];try{{Array.from(el.childNodes||[]).forEach(function(n){{if(n.nodeType===3){{var t=String(n.textContent||'').replace(/\\s+/g,' ').trim();if(t)out.push(t);}}}});}}catch(e){{}}return out.join(' ').trim().slice(0,240);}}
function contentCandidate(el,actionable){{if(actionable)return true;var tag=(el.tagName||'').toLowerCase();if(/^h[1-6]$/.test(tag))return true;if(tag==='img'&&(el.getAttribute('alt')||'').trim())return true;var own=ownText(el);if(own.length>=2)return true;var cls=String(el.className||'').toLowerCase(),id=String(el.id||'').toLowerCase();var cardish=/(^|[-_ ])(card|item|listing|result|row|advert|product|property)([-_ ]|$)/.test(cls+' '+id);if((tag==='article'||tag==='tr'||tag==='li'||cardish)){{var txt=__mcpText(el);if(txt.length>=2&&txt.length<=700)return true;}}return false;}}
var s=__mcpState();Object.keys(s.elements).forEach(function(k){{var e=s.elements[k];if(!e||!e.isConnected)delete s.elements[k];}});
var scope={scope_json},all=Array.from(document.querySelectorAll('*')),elements=[];
for(var i=0;i<all.length&&elements.length<{int(max_elements)};i++){{var el=all[i];if(!__mcpVisible(el))continue;var actionable=__mcpActionable(el);if(scope==='interactive'&&!actionable)continue;if(scope==='visible'&&!actionable){{var txt=__mcpText(el);if(!txt||txt.length<2)continue;}}if((scope==='content'||scope==='leaf')&&!contentCandidate(el,actionable))continue;var desc=__mcpDescribe(el,s);if((scope==='content'||scope==='leaf')&&!actionable){{var own=ownText(el);if(own)desc.text=own;if(!desc.text&&(el.getAttribute('alt')||''))desc.text=String(el.getAttribute('alt')).slice(0,240);}}elements.push(desc);}}
var obs='bobs_'+s.pageToken+'_'+Date.now().toString(36);s.observations[obs]=s.mutationRevision;
return JSON.stringify({{ok:true,observation_id:obs,dom_revision:s.mutationRevision,url:location.href,title:document.title,scroll:{{x:scrollX,y:scrollY}},viewport:{{w:innerWidth,h:innerHeight}},scope:scope,element_count:elements.length,elements:elements}});
}})()'''


def _capture(target: Dict[str, Any], visual: str, element: Optional[Dict[str, Any]] = None) -> Optional[Image]:
    if visual == "none":
        return None
    params: Dict[str, Any] = {"format": "jpeg", "quality": 65, "fromSurface": True}
    if visual == "full_page":
        params["captureBeyondViewport"] = True
    elif visual == "element":
        if not element:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "element_id is required when visual='element'")
        rect = element.get("document_rect") or {}
        params["clip"] = {
            "x": max(0, float(rect.get("x", 0))),
            "y": max(0, float(rect.get("y", 0))),
            "width": max(1, float(rect.get("w", 1))),
            "height": max(1, float(rect.get("h", 1))),
            "scale": 1,
        }
    result = _cdp_call(target, "Page.captureScreenshot", params, timeout_s=20)
    import base64
    data = base64.b64decode(result.get("data") or "")
    return Image(data=data, format="jpeg") if data else None


def browser_observe(scope: str = "interactive", max_elements: int = 40,
                    visual: str = "none", element_id: Optional[str] = None,
                    tab_id: Optional[str] = None) -> Any:
    scope = (scope or "interactive").strip().lower()
    visual = (visual or "none").strip().lower()
    if scope not in {"interactive", "visible", "content", "leaf"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "scope must be interactive, visible, content, or leaf")
    if visual not in {"none", "viewport", "element", "full_page"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "visual must be none, viewport, element, or full_page")
    max_elements = max(1, min(int(max_elements), 200))
    target = _target(tab_id)
    raw = _evaluate(target, _observe_js(scope, max_elements))
    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not decode Chromium observation: {exc}") from exc
    payload["tab_id"] = target.get("id")
    match = None
    if element_id:
        match = next((e for e in payload.get("elements", []) if e.get("element_id") == element_id), None)
        if visual == "element" and not match:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"element_id not found in this observation: {element_id}")
    image = _capture(target, visual, match)
    if image:
        compact = dict(payload)
        compact["visual"] = visual
        return [json.dumps(compact, ensure_ascii=False, indent=2), image]
    return payload


def _score_element(element: Dict[str, Any], query: str, role: Optional[str], text: Optional[str]) -> float:
    erole = str(element.get("role") or "").lower()
    if role and erole != role.lower():
        return -1
    if text:
        hay = " ".join(str(element.get(k) or "") for k in ("text", "aria_label", "placeholder", "title", "name")).lower()
        if text.lower() not in hay:
            return -1
    q = query.strip().lower()
    if not q:
        return 0.0
    fields = [
        str(element.get("text") or ""), str(element.get("aria_label") or ""),
        str(element.get("placeholder") or ""), str(element.get("name") or ""),
        str(element.get("title") or ""), erole, str(element.get("tag") or ""),
    ]
    best = 0.0
    for value in fields:
        v = value.strip().lower()
        if not v:
            continue
        if v == q:
            best = max(best, 100.0)
        elif v.startswith(q):
            best = max(best, 80.0)
        elif q in v:
            best = max(best, 60.0)
        else:
            qwords = set(q.split())
            vwords = set(v.split())
            if qwords:
                best = max(best, 40.0 * len(qwords & vwords) / len(qwords))
    if q and best <= 0:
        return -1
    if element.get("actionable"):
        best += 5
    return best


def browser_find(query: str, role: Optional[str] = None, text: Optional[str] = None,
                 tab_id: Optional[str] = None, max_results: int = 5,
                 actionable_only: bool = False) -> Dict[str, Any]:
    observe_scope = "interactive" if actionable_only or role else "visible"
    observed = browser_observe(scope=observe_scope, max_elements=200, visual="none", tab_id=tab_id)
    elements = observed.get("elements", [])
    ranked = []
    for element in elements:
        if actionable_only and not element.get("actionable"):
            continue
        score = _score_element(element, query, role, text)
        if score >= 0:
            ranked.append((score, element))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    matches = [{**element, "match_score": round(score, 2)} for score, element in ranked[:max(1, min(max_results, 20))] if score > 0 or not query]
    return {
        "ok": True,
        "tab_id": observed.get("tab_id"),
        "observation_id": observed.get("observation_id"),
        "query": query,
        "count": len(matches),
        "matches": matches,
        "best_match": matches[0] if matches else None,
    }


def _resolve_actions(tab_id: Optional[str], actions: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], Optional[str]]:
    resolved = []
    observation_id = None
    for action in actions:
        item = dict(action)
        if not item.get("element_id") and any(item.get(k) for k in ("query", "target", "text_match", "role")):
            query = str(item.get("query") or item.get("target") or item.get("text_match") or item.get("role") or "")
            found = browser_find(query=query, role=item.get("role"), text=item.get("text_match"), tab_id=tab_id, max_results=1, actionable_only=True)
            best = found.get("best_match")
            if not best:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"No browser element matched action target: {query}")
            item["element_id"] = best.get("element_id")
            observation_id = found.get("observation_id")
        resolved.append(item)
    return resolved, observation_id


def _batch_js(actions: List[Dict[str, Any]], observation_id: Optional[str]) -> str:
    actions_json = json.dumps(actions, ensure_ascii=False)
    obs_json = json.dumps(observation_id)
    return f'''(function(){{
{_BOOTSTRAP}
var s=__mcpState(),expected={obs_json};
if(expected&&!(expected in s.observations))return JSON.stringify({{ok:false,error:'stale_observation',observe_again:true}});
var changed=expected?(s.observations[expected]!==s.mutationRevision):false,actions={actions_json},results=[];
function target(id){{var el=s.elements[id];return (el&&el.isConnected)?el:null;}}
function emit(el,type){{try{{el.dispatchEvent(new Event(type,{{bubbles:true}}));}}catch(e){{}}}}
function setValue(el,value){{var tag=(el.tagName||'').toLowerCase();if(el.isContentEditable){{el.focus();el.textContent=value;try{{el.dispatchEvent(new InputEvent('input',{{bubbles:true,inputType:'insertText',data:value}}));}}catch(e){{emit(el,'input');}}return;}}var proto=tag==='textarea'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;var d=Object.getOwnPropertyDescriptor(proto,'value');if(d&&d.set)d.set.call(el,value);else el.value=value;emit(el,'input');emit(el,'change');}}
for(var i=0;i<actions.length;i++){{var a=actions[i]||{{}},type=String(a.type||'').toLowerCase().replace(/-/g,'_'),el=a.element_id?target(a.element_id):null;if(a.element_id&&!el){{results.push({{index:i,type:type,element_id:a.element_id,ok:false,error:'stale_element',observe_again:true}});break;}}try{{
if(type==='click'||type==='double_click'){{if(!el)throw new Error('element_id is required');el.scrollIntoView({{block:'center',inline:'nearest'}});var tag=(el.tagName||'').toLowerCase(),href=String(el.getAttribute('href')||''),inputType=String(el.getAttribute('type')||'').toLowerCase();var mayNavigate=(tag==='a'&&href&&href!=='#')||((tag==='button'||tag==='input')&&inputType==='submit');var shouldDefer=(type==='click'&&i===actions.length-1&&mayNavigate);if(type==='double_click')el.dispatchEvent(new MouseEvent('dblclick',{{bubbles:true,cancelable:true,view:window}}));else if(shouldDefer)setTimeout(function(){{try{{el.click();}}catch(e){{}}}},0);else el.click();results.push({{index:i,type:type,element_id:a.element_id,ok:true,deferred:shouldDefer}});}}
else if(type==='type'||type==='type_text'||type==='paste'){{if(!el)throw new Error('element_id is required');el.focus();var value=String(a.text==null?'':a.text);if(a.clear===false){{var current=el.isContentEditable?String(el.textContent||''):String(el.value||'');value=current+value;}}setValue(el,value);results.push({{index:i,type:type,element_id:a.element_id,ok:true,value:(el.isContentEditable?String(el.textContent||''):String(el.value||'')).slice(0,200)}});}}
else if(type==='select'){{if(!el)throw new Error('element_id is required');var wanted=String(a.option==null?'':a.option).trim().toLowerCase(),chosen=null;if((el.tagName||'').toLowerCase()==='select'){{var opts=Array.from(el.options||[]);chosen=opts.find(function(o){{return String(o.value).toLowerCase()===wanted||String(o.text).trim().toLowerCase()===wanted;}})||opts.find(function(o){{return String(o.text).trim().toLowerCase().includes(wanted);}});if(!chosen)throw new Error('option_not_found');el.value=chosen.value;emit(el,'input');emit(el,'change');}}else{{el.click();var c=Array.from(document.querySelectorAll('[role="option"],option,[role="menuitem"],li,button,a')).filter(__mcpVisible);chosen=c.find(function(o){{return __mcpText(o).toLowerCase()===wanted;}})||c.find(function(o){{return __mcpText(o).toLowerCase().includes(wanted);}});if(!chosen)throw new Error('option_not_found');chosen.click();}}results.push({{index:i,type:type,element_id:a.element_id,ok:true,selected:chosen?__mcpText(chosen):wanted}});}}
else if(type==='scroll'){{if(el)el.scrollIntoView({{block:String(a.block||'center'),inline:'nearest'}});else window.scrollBy(Number(a.dx||0),Number(a.dy||300));results.push({{index:i,type:type,element_id:a.element_id||null,ok:true}});}}
else if(type==='focus'){{if(!el)throw new Error('element_id is required');el.focus();results.push({{index:i,type:type,element_id:a.element_id,ok:true}});}}
else if(type==='check'||type==='uncheck'){{if(!el)throw new Error('element_id is required');var wanted=type==='check';if(!!el.checked!==wanted)el.click();results.push({{index:i,type:type,element_id:a.element_id,ok:true,checked:!!el.checked}});}}
else throw new Error('unsupported_action:'+type);
}}catch(e){{results.push({{index:i,type:type,element_id:a.element_id||null,ok:false,error:String(e&&e.message||e)}});break;}}}}
return JSON.stringify({{ok:results.every(function(r){{return r.ok;}}),actions:results,dom_changed_since_observe:changed,dom_revision:s.mutationRevision,url:location.href,title:document.title,scroll:{{x:scrollX,y:scrollY}}}});
}})()'''


def browser_act(actions: List[Dict[str, Any]], observation_id: Optional[str] = None,
                tab_id: Optional[str] = None, return_state: str = "compact") -> Dict[str, Any]:
    if not isinstance(actions, list) or not actions:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "actions must contain at least one browser action")
    if len(actions) > 20:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "browser_act supports at most 20 actions per call")
    return_state = (return_state or "compact").lower()
    if return_state not in {"none", "compact", "full"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "return_state must be none, compact, or full")
    target = _target(tab_id)
    resolved, inferred_observation = _resolve_actions(str(target.get("id")), actions)
    expected = observation_id or inferred_observation
    raw = _evaluate(target, _batch_js(resolved, expected), timeout_s=20)
    try:
        result = json.loads(raw)
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not decode browser action result: {exc}") from exc
    result["tab_id"] = target.get("id")
    if return_state != "none":
        state = browser_observe(scope="interactive" if return_state == "compact" else "visible",
                                max_elements=40 if return_state == "compact" else 120,
                                visual="none", tab_id=str(target.get("id")))
        result["state"] = state
    return result


def _validate_browser_url(url: str) -> str:
    url = str(url or "").strip()
    if url == "about:blank":
        return url
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Browser URL must be http://, https://, or about:blank")
    return url


def browser_open_url(url: str, tab_id: Optional[str] = None, new_tab: bool = False,
                     activate: bool = True) -> Dict[str, Any]:
    url = _validate_browser_url(url)
    if new_tab:
        result = _cdp_call(_browser_target(), "Target.createTarget", {"url": url, "newWindow": False}, timeout_s=12)
        created = str(result.get("targetId") or "")
        if not created:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Chromium did not return a targetId for the new tab")
        if activate:
            browser_activate_tab(created)
        return {"ok": True, "tab_id": created, "url": url, "new_tab": True, "via": "cdp_headed_chromium"}
    target = _target(tab_id)
    result = _cdp_call(target, "Page.navigate", {"url": url}, timeout_s=15)
    if activate:
        browser_activate_tab(str(target.get("id")))
    return {"ok": True, "tab_id": target.get("id"), "url": url, "frame_id": result.get("frameId"), "new_tab": False, "via": "cdp_headed_chromium"}


_SEMANTIC_EXTRACT_ALIASES = {
    "price": ["price", "fiyat", "total", "toplam", "tutar", "₺", "tl", "try", "€", "eur", "$", "usd", "£", "gbp"],
    "cancellation": ["cancellation", "cancel", "refundable", "refund", "free cancellation", "iptal", "ücretsiz iptal", "ucretsiz iptal", "iade"],
    "parking": ["parking", "car park", "parking lot", "otopark", "park yeri", "valet", "vale"],
    "rating": ["rating", "score", "review score", "puan", "değerlendirme", "degerlendirme", "yorum puanı", "yorum puani"],
    "breakfast": ["breakfast", "kahvaltı", "kahvalti"],
    "payment": ["payment", "pay at property", "pay later", "prepayment", "ödeme", "odeme", "otelde ödeme", "tesiste ödeme"],
    "location": ["location", "address", "konum", "adres"],
    "distance": ["distance", "away", "walking", "walk", "mesafe", "uzaklık", "uzaklik", "yürüme", "yurume"],
    "availability": ["availability", "available", "rooms left", "müsait", "musait", "son oda", "son odalar"],
    "checkin": ["check-in", "check in", "giriş", "giris"],
    "checkout": ["check-out", "check out", "çıkış", "cikis"],
}


def _wait_browser(tab_id: str, condition: str = "network_idle", timeout_s: float = 15.0,
                  selector: Optional[str] = None, text: Optional[str] = None,
                  initial_url: Optional[str] = None, stable_ms: int = 500,
                  required: bool = True) -> Dict[str, Any]:
    condition = str(condition or "network_idle").strip().lower()
    if condition not in {"network_idle", "dom_stable", "selector", "text", "url_change"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "wait condition must be network_idle, dom_stable, selector, text, or url_change")
    timeout_s = max(0.1, min(float(timeout_s), 60.0))
    stable_ms = max(100, min(int(stable_ms), 5000))
    target = _target(tab_id)
    started = time.perf_counter()
    last_signature = None
    stable_since = time.perf_counter()
    polls = 0
    matched = False
    final_state: Dict[str, Any] = {}
    while time.perf_counter() - started < timeout_s:
        selector_js = json.dumps(str(selector or ""))
        text_js = json.dumps(str(text or "").lower())
        initial_js = json.dumps(str(initial_url or ""))
        expression = f'''(function(){{
{_BOOTSTRAP}
var s=__mcpState();
var state={{ready:document.readyState,url:location.href,title:document.title,rev:s.mutationRevision,resources:(performance.getEntriesByType('resource')||[]).length}};
state.selector=({selector_js})?!!document.querySelector({selector_js}):false;
state.text=({text_js})?String((document.body&&document.body.innerText)||'').toLowerCase().indexOf({text_js})>=0:false;
state.url_changed=({initial_js})?location.href!=={initial_js}:false;
return JSON.stringify(state);
}})()'''
        raw = _evaluate(target, expression, timeout_s=min(8.0, timeout_s))
        polls += 1
        try:
            state = json.loads(raw)
        except Exception:
            state = {}
        final_state = state
        now = time.perf_counter()
        if condition == "selector":
            matched = bool(state.get("selector"))
        elif condition == "text":
            matched = bool(state.get("text"))
        elif condition == "url_change":
            matched = bool(state.get("url_changed"))
        else:
            signature = state.get("resources") if condition == "network_idle" else state.get("rev")
            if signature != last_signature:
                last_signature = signature
                stable_since = now
            stable_for_ms = (now - stable_since) * 1000
            if condition == "network_idle":
                matched = state.get("ready") == "complete" and stable_for_ms >= stable_ms
            else:
                matched = stable_for_ms >= stable_ms
        if matched:
            break
        time.sleep(0.125)
    duration_ms = int((time.perf_counter() - started) * 1000)
    result = {
        "ok": bool(matched or not required),
        "type": "wait",
        "for": condition,
        "matched": bool(matched),
        "timed_out": not matched,
        "required": bool(required),
        "duration_ms": duration_ms,
        "polls": polls,
        "url": final_state.get("url"),
        "title": final_state.get("title"),
    }
    if not matched and required:
        result["error"] = f"Timed out waiting for {condition}"
    return result


def _normalize_extract_fields(fields: List[Any]) -> List[Dict[str, Any]]:
    if not isinstance(fields, list) or not fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "extract fields must be a non-empty list")
    if len(fields) > 20:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "extract supports at most 20 fields")
    normalized: List[Dict[str, Any]] = []
    for index, field in enumerate(fields):
        if isinstance(field, str):
            name = field.strip()
            if not name:
                continue
            normalized.append({"name": name, "semantic": name, "all": True, "max_items": 2})
        elif isinstance(field, dict):
            item = dict(field)
            item.setdefault("name", f"field_{index + 1}")
            normalized.append(item)
        else:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "extract fields must be strings or field objects")
    if not normalized:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "extract fields cannot be empty")
    return normalized


def _extract_browser(tab_id: str, fields: List[Any], max_chars: int = 6000) -> Dict[str, Any]:
    specs = _normalize_extract_fields(fields)
    aliases = json.dumps(_SEMANTIC_EXTRACT_ALIASES, ensure_ascii=False)
    specs_js = json.dumps(specs, ensure_ascii=False)
    budget = max(256, min(int(max_chars), 20_000))
    expression = f'''(function(){{
var specs={specs_js},aliases={aliases},budget={budget},used=0,truncated=false,data={{}},counts={{}};
function clean(v){{return String(v==null?'':v).replace(/\\s+/g,' ').trim();}}
function norm(v){{return clean(v).toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').replace(/[ıİ]/g,'i');}}
function visible(el){{try{{var s=getComputedStyle(el),r=el.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity||1)!==0&&r.width>1&&r.height>1;}}catch(e){{return false;}}}}
function bounded(v){{v=clean(v);var remain=Math.max(0,budget-used);if(v.length>remain){{v=v.slice(0,remain);truncated=true;}}used+=v.length;return v;}}
function read(el,attr){{attr=String(attr||'text');if(attr==='text')return clean(el.innerText||el.textContent||'');if(attr==='value')return clean(el.value);if(attr==='href')return clean(el.href||el.getAttribute('href'));if(attr==='html')return clean(el.innerHTML);return clean(el.getAttribute(attr));}}
var candidates=Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,li,dt,dd,label,button,a,span,strong,b,small,div')).filter(visible).slice(0,6000).map(function(el){{return {{el:el,raw:clean(el.innerText||el.textContent||''),tag:String(el.tagName||'').toLowerCase()}};}}).filter(function(x){{return x.raw&&x.raw.length<=520;}});
for(var i=0;i<specs.length;i++){{
 var sp=specs[i]||{{}},name=String(sp.name||('field_'+i)),vals=[];
 if(sp.selector){{
   var els=[];try{{els=Array.from(document.querySelectorAll(String(sp.selector)));}}catch(e){{els=[];}}
   var maxItems=Math.max(1,Math.min(Number(sp.max_items||20),100));vals=els.slice(0,maxItems).map(function(el){{return read(el,sp.attr);}}).filter(Boolean);counts[name]=els.length;
 }} else {{
   var key=norm(sp.semantic||name),terms=[sp.semantic||name];Object.keys(aliases).forEach(function(k){{var nk=norm(k);if(key===nk||key.indexOf(nk)>=0||nk.indexOf(key)>=0)terms=terms.concat(aliases[k]||[]);}});terms=terms.map(norm).filter(Boolean);
   var ranked=[];
   for(var c=0;c<candidates.length;c++){{var raw=candidates[c].raw,n=norm(raw),score=0;for(var t=0;t<terms.length;t++){{if(n===terms[t])score=Math.max(score,180);else if(n.indexOf(terms[t])>=0)score=Math.max(score,110+Math.min(30,terms[t].length));}}
     if(key.indexOf('price')>=0||key.indexOf('fiyat')>=0){{if(/[₺€$£]|\\b(?:tl|try|eur|usd|gbp)\\b/i.test(raw)&&/\\d/.test(raw))score=Math.max(score,165);}}
     if(key.indexOf('rating')>=0||key.indexOf('score')>=0||key.indexOf('puan')>=0){{if(/^\\s*(?:[0-9](?:[.,][0-9])?|10(?:[.,]0)?)\\s*(?:\\/\\s*(?:5|10))?\\s*$/.test(raw))score=Math.max(score,155);}}
     if(score>0){{if(raw.length<=100)score+=20;ranked.push({{score:score,text:raw}});}}
   }}
   ranked.sort(function(a,b){{return b.score-a.score||a.text.length-b.text.length;}});var seen={{}},maxItems=Math.max(1,Math.min(Number(sp.max_items||2),20));for(var r=0;r<ranked.length&&vals.length<maxItems;r++){{var sig=norm(ranked[r].text);if(!seen[sig]){{seen[sig]=true;vals.push(ranked[r].text);}}}}counts[name]=vals.length;
 }}
 vals=vals.map(bounded);data[name]=sp.all===false?(vals[0]||null):vals;
}}
return JSON.stringify({{ok:true,type:'extract',url:location.href,title:document.title,data:data,matched_counts:counts,truncated:truncated,chars:used}});
}})()'''
    raw = _evaluate(_target(tab_id), expression, timeout_s=20)
    try:
        return json.loads(raw)
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not decode browser extract result: {exc}") from exc


def browser_do(url: Optional[str] = None, actions: Optional[List[Dict[str, Any]]] = None,
               tab_id: Optional[str] = None, new_tab: bool = True, activate: bool = True,
               wait_after_open: bool = True, return_state: str = "none",
               close_after: bool = False, debug: bool = False,
               extract: Optional[List[Any]] = None) -> Dict[str, Any]:
    started = time.perf_counter()
    if close_after and not (url and new_tab):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "close_after is only allowed when browser_do opens a new tab")
    requested_actions = list(actions or [])
    automatic_actions = (1 if url and wait_after_open else 0) + (1 if extract is not None else 0)
    if len(requested_actions) + automatic_actions > 20:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "browser_do supports at most 20 actions including automatic wait/extract")
    return_state = str(return_state or "none").strip().lower()
    if return_state not in {"none", "compact", "full"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "return_state must be none, compact, or full")

    opened: Optional[Dict[str, Any]] = None
    created_new_tab = False
    current_tab = tab_id
    initial_url = None
    final_url: Optional[str] = None
    final_title: Optional[str] = None
    results: List[Dict[str, Any]] = []
    data: Dict[str, Any] = {}

    if url:
        opened = browser_open_url(url=url, tab_id=current_tab, new_tab=new_tab, activate=activate)
        current_tab = str(opened.get("tab_id") or current_tab or "")
        created_new_tab = bool(opened.get("new_tab"))
        initial_url = str(url)
        final_url = str(opened.get("url") or url)
        if wait_after_open:
            waited = _wait_browser(current_tab, "network_idle", timeout_s=15, required=False)
            results.append(waited)
            final_url = waited.get("url") or final_url
            final_title = waited.get("title") or final_title
    elif not current_tab:
        current_tab = str(_target().get("id"))

    for action in requested_actions:
        action_type = str(action.get("type") or "").strip().lower().replace("-", "_")
        if action_type == "wait":
            result = _wait_browser(
                current_tab,
                condition=str(action.get("for") or action.get("condition") or "network_idle"),
                timeout_s=float(action.get("timeout_s", 10)),
                selector=action.get("selector"), text=action.get("text"),
                initial_url=str(action.get("initial_url") or initial_url or ""),
                stable_ms=int(action.get("stable_ms", 500)),
                required=bool(action.get("required", True)),
            )
        elif action_type == "extract":
            result = _extract_browser(current_tab, action.get("fields") or [], int(action.get("max_chars", 6000)))
            if isinstance(result.get("data"), dict):
                data.update(result["data"])
        else:
            result = browser_act([action], tab_id=current_tab, return_state="none")
            # Flatten the single semantic action so browser_do debug output stays compact.
            if isinstance(result.get("actions"), list) and result["actions"]:
                action_result = dict(result["actions"][0])
                action_result.setdefault("url", result.get("url"))
                action_result.setdefault("title", result.get("title"))
                result = action_result
        results.append(result)
        final_url = result.get("url") or final_url
        final_title = result.get("title") or final_title
        if result.get("ok") is False:
            break

    if extract is not None and (not results or results[-1].get("ok") is not False):
        extracted = _extract_browser(current_tab, extract, 6000)
        results.append(extracted)
        final_url = extracted.get("url") or final_url
        final_title = extracted.get("title") or final_title
        if isinstance(extracted.get("data"), dict):
            data.update(extracted["data"])
    elif not requested_actions and extract is None and (not results or results[-1].get("ok") is not False):
        extracted = _extract_browser(current_tab, [
            {"name": "h1", "selector": "h1", "attr": "text", "all": False, "max_items": 1},
            {"name": "paragraphs", "selector": "p", "attr": "text", "all": True, "max_items": 20},
        ], 3000)
        results.append(extracted)
        final_url = extracted.get("url") or final_url
        final_title = extracted.get("title") or final_title
        data.update(extracted.get("data") or {})

    state = None
    if return_state != "none" and current_tab:
        state = browser_observe(
            scope="interactive" if return_state == "compact" else "visible",
            max_elements=40 if return_state == "compact" else 120,
            visual="none", tab_id=current_tab,
        )

    closed = False
    if close_after:
        if not created_new_tab or not current_tab:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "browser_do did not create the tab it was asked to close")
        closed = bool(browser_close_tab(current_tab).get("closed"))

    ok = all(item.get("ok", True) is not False for item in results)
    compact: Dict[str, Any] = {
        "ok": ok,
        "data": data,
        "url": ((state or {}).get("url") if isinstance(state, dict) else None) or final_url,
        "title": ((state or {}).get("title") if isinstance(state, dict) else None) or final_title,
        "tab_id": current_tab,
        "action_count": len(results),
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "closed": closed,
    }
    if opened:
        compact["opened"] = {k: opened.get(k) for k in ("tab_id", "url", "new_tab")}
    errors = [
        {k: item.get(k) for k in ("type", "error", "for", "timed_out") if item.get(k) is not None}
        for item in results if item.get("ok") is False
    ]
    if errors:
        compact["errors"] = errors
    if state is not None:
        compact["state"] = state
    if debug:
        compact["actions"] = results
    return compact
