"""Earth Engine REST transport; no ee SDK or end-user installation.
Credentials belong to the deployed agent, never to the task payload.
"""
import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse
class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep bearer tokens on the configured host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

ASSET='https://earthengine.googleapis.com/v1/projects/earthengine-public/assets/MERIT/Hydro/v1_0_1'
class EarthEngineREST:
    def __init__(self,project):
        self.project=project or os.environ.get('EE_PROJECT','')
        if not self.project:raise ValueError('后台尚未配置 EE_PROJECT；请联系部署者，终端用户无需安装软件')
        self.token='';self.expires=0
    def access_token(self):
        if self.token and time.monotonic()<self.expires:return self.token
        refresh=os.environ.get('EE_REFRESH_TOKEN','')
        if refresh:
            fields={k:os.environ.get(e,'') for k,e in [('client_id','EE_CLIENT_ID'),('client_secret','EE_CLIENT_SECRET')]}
            fields.update(refresh_token=refresh,grant_type='refresh_token')
            req=urllib.request.Request('https://oauth2.googleapis.com/token',data=urllib.parse.urlencode(fields).encode())
            try:
                with urllib.request.build_opener(NoRedirect()).open(req,timeout=30) as r:reply=json.load(r)
            except Exception:raise ValueError('后台 Earth Engine 凭证刷新失败，请联系部署者') from None
            self.token=reply['access_token'];self.expires=time.monotonic()+max(0,int(reply.get('expires_in',3600))-60)
        else:
            self.token=os.environ.get('EE_ACCESS_TOKEN','')
            self.expires=time.monotonic()+60
        if not self.token:raise ValueError('后台尚未配置 Earth Engine 授权；终端用户无需安装或登录软件')
        return self.token
    def request(self,suffix='',payload=None):
        for attempt in range(3):
            req=urllib.request.Request(ASSET+suffix,data=None if payload is None else json.dumps(payload).encode(),
                headers={'Authorization':'Bearer '+self.access_token(),'x-goog-user-project':self.project,'Content-Type':'application/json'})
            try:
                with urllib.request.build_opener(NoRedirect()).open(req,timeout=90) as r:
                    raw=r.read(48*1024**2+1)
                if len(raw)>48*1024**2:raise ValueError('下载响应超过上限')
                return raw
            except urllib.error.HTTPError as exc:
                status=exc.code;exc.close()
                if status==401 and attempt<2 and os.environ.get('EE_REFRESH_TOKEN'):
                    self.expires=0;continue
                if status not in (429,500,502,503,504):
                    raise ValueError(f'Earth Engine HTTP {status}；请由部署者核对项目授权与配额') from None
            except (urllib.error.URLError,TimeoutError,OSError):pass
            if attempt<2:time.sleep(2**attempt)
        raise ConnectionError('Earth Engine 请求失败，已进行有限重试')
