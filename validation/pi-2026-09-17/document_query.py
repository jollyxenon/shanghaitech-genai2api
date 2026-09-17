"""供 Pi 内置 Bash 使用的普通 PDF 问答命令；调用真实代理文档接口。"""
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path


def main():
    """上传命令行指定 PDF，返回模型回答而不是本地提取文本。"""
    path=Path(sys.argv[1])
    question=sys.argv[2]
    data={'model':'deepseek-pro','stream':False,'max_output_tokens':1600,'reasoning':{'effort':'none'},'input':[{'role':'user','content':[{'type':'input_text','text':question},{'type':'input_file','filename':path.name,'file_data':'data:application/pdf;base64,'+base64.b64encode(path.read_bytes()).decode()}]}]}
    headers={'Content-Type':'application/json','Authorization':'Bearer '+os.environ.get('PI_GENAI_API_KEY','local-validation')}
    request=urllib.request.Request('http://127.0.0.1:31100/v1/responses',data=json.dumps(data).encode(),headers=headers)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request,timeout=315) as response:
        result=json.load(response)
    print(json.dumps({'status':result['status'],'answer':'\n'.join(part.get('text','') for item in result['output'] for part in item.get('content',[]))},ensure_ascii=False))


if __name__=='__main__':
    main()
