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
function __mcpVisible(el){if(!el||el.nodeType!==1)return false;var st=getComputedStyle(el);if(st.display==='none'||st.visibility==='hidden'||parseFloat(st.opacity||'1')===0)return false;var r=el.getBoundingClientRect();if(r.width<1||r.height<1)return false;return r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth;}
function __mcpActionable(el){var tag=(el.tagName||'').toLowerCase(),role=(el.getAttribute('role')||'').toLowerCase();if(['a','button','input','textarea','select','summary','details'].includes(tag))return true;if(['button','link','checkbox','radio','tab','menuitem','option','combobox','textbox','searchbox','switch','slider'].includes(role))return true;if(el.isContentEditable||el.hasAttribute('onclick'))return true;try{if(getComputedStyle(el).cursor==='pointer')return true;}catch(e){}var ti=el.getAttribute('tabindex');return ti!==null&&Number(ti)>=0;}
function __mcpText(el){var aria=el.getAttribute('aria-label')||'',ph=el.getAttribute('placeholder')||'',title=el.getAttribute('title')||'',txt='';try{txt=(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim();}catch(e){}return (aria||ph||title||txt).slice(0,240);}
function __mcpRole(el){var role=el.getAttribute('role');if(role)return role;var tag=(el.tagName||'').toLowerCase();if(tag==='a')return 'link';if(tag==='button')return 'button';if(tag==='select')return 'combobox';if(tag==='textarea'||el.isContentEditable)return 'textbox';if(tag==='input'){var t=(el.type||'text').toLowerCase();if(t==='checkbox')return 'checkbox';if(t==='radio')return 'radio';if(['button','submit','reset'].includes(t))return 'button';return 'textbox';}return '';}
function __mcpRect(el){var r=el.getBoundingClientRect();return {viewport:{x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),h:Math.round(r.height)},document:{x:Math.round(r.left+scrollX),y:Math.round(r.top+scrollY),w:Math.round(r.width),h:Math.round(r.height)}};}
function __mcpDescribe(el,s){var tag=(el.tagName||'').toLowerCase(),rect=__mcpRect(el);var out={element_id:__mcpId(el,s),tag:tag,role:__mcpRole(el),text:__mcpText(el),viewport_rect:rect.viewport,document_rect:rect.document,actionable:__mcpActionable(el)};var aria=el.getAttribute('aria-label')||'',ph=el.getAttribute('placeholder')||'',name=el.getAttribute('name')||'',title=el.getAttribute('title')||'';if(aria)out.aria_label=aria.slice(0,120);if(ph)out.placeholder=ph.slice(0,120);if(name)out.name=name.slice(0,100);if(title)out.title=title.slice(0,120);if(tag==='a'&&el.href)out.href=String(el.href).slice(0,240);if(el.disabled===true)out.enabled=false;else out.enabled=true;if(document.activeElement===el)out.focused=true;if(['input','textarea','select'].includes(tag))out.value=String(el.value||'').slice(0,200);if(tag==='input'&&el.type)out.input_type=String(el.type);if(typeof el.checked==='boolean')out.checked=!!el.checked;if(tag==='select')out.options=Array.from(el.options||[]).slice(0,30).map(function(o){return {text:String(o.text||'').slice(0,100),value:String(o.value||'').slice(0,100),selected:!!o.selected};});return out;}
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
    if element.get("actionable"):
        best += 5
    return best


def browser_find(query: str, role: Optional[str] = None, text: Optional[str] = None,
                 tab_id: Optional[str] = None, max_results: int = 5,
                 actionable_only: bool = False) -> Dict[str, Any]:
    observed = browser_observe(scope="visible", max_elements=200, visual="none", tab_id=tab_id)
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
