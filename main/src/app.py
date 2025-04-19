from flask import Flask, request, render_template, Response, abort, make_response, redirect, session
import requests
from urllib.parse import urlparse, urljoin, quote, unquote
import json
import os
import re
import chardet
from bs4 import BeautifulSoup
import mimetypes
import uuid
import time
from urllib.parse import parse_qs, urlencode

app = Flask(__name__, template_folder='../templates', static_folder='../static')
app.secret_key = os.urandom(24)  # セッション用のシークレットキー

# セッション管理用の辞書
sessions = {}

def clean_old_sessions():
    """古いセッションを削除"""
    current_time = time.time()
    for session_id in list(sessions.keys()):
        if current_time - sessions[session_id]['timestamp'] > 3600:  # 1時間以上経過したセッションを削除
            del sessions[session_id]

def get_or_create_session():
    """セッションの取得または作成"""
    session_id = session.get('session_id')
    if not session_id or session_id not in sessions:
        session_id = str(uuid.uuid4())
        session['session_id'] = session_id
        sessions[session_id] = {
            'timestamp': time.time(),
            'cookies': {},
            'headers': {}
        }
    return session_id

def update_session_data(session_id, response):
    """セッションデータの更新"""
    if session_id in sessions:
        sessions[session_id]['timestamp'] = time.time()
        for cookie in response.cookies:
            sessions[session_id]['cookies'][cookie.name] = {
                'value': cookie.value,
                'domain': cookie.domain,
                'path': cookie.path,
                'expires': cookie.expires,
                'secure': cookie.secure
            }

def process_css_content(css_content, base_url):
    """CSSファイル内のURLをプロキシ経由に変換する"""
    if not css_content:
        return css_content
    
    url_pattern = r'url\(["\']?((?!data:)[^)"\']+)["\']?\)'
    def replace_url(match):
        url = match.group(1)
        if url.startswith(('http://', 'https://')):
            return f'url(/proxy?url={quote(url)})'
        full_url = urljoin(base_url, url)
        return f'url(/proxy?url={quote(full_url)})'
    return re.sub(url_pattern, replace_url, css_content)

def detect_encoding(response):
    # Content-Typeヘッダーからエンコーディングを取得
    content_type = response.headers.get('content-type', '').lower()
    charset_match = re.search(r'charset=([\w-]+)', content_type)
    if charset_match:
        return charset_match.group(1)

    # メタタグからエンコーディングを取得
    if 'text/html' in content_type:
        try:
            content = response.content
            soup = BeautifulSoup(content, 'html.parser')
            meta_charset = soup.find('meta', charset=True)
            if meta_charset:
                return meta_charset.get('charset')
                
            meta_content_type = soup.find('meta', {'http-equiv': lambda x: x and x.lower() == 'content-type'})
            if meta_content_type:
                charset_match = re.search(r'charset=([\w-]+)', meta_content_type.get('content', '').lower())
                if charset_match:
                    return charset_match.group(1)
        except:
            pass

    # chardetを使用してエンコーディングを推測
    try:
        if not response.content:
            return 'utf-8'
        detected = chardet.detect(response.content)
        if detected and detected['confidence'] > 0.7:
            return detected['encoding']
    except:
        pass

    # デフォルトのエンコーディング
    return 'utf-8'

def modify_html_content(content, base_url):
    soup = BeautifulSoup(content, 'html.parser')
    
    # 処理対象のタグと属性
    url_attributes = {
        'a': ['href'],
        'img': ['src', 'srcset'],
        'link': ['href'],
        'script': ['src'],
        'iframe': ['src'],
        'form': ['action'],
        'meta': ['content'],  # Open Graph URLなど
        'video': ['src', 'poster'],
        'source': ['src', 'srcset'],
        'object': ['data'],
        'embed': ['src'],
    }

    def process_srcset(srcset_value):
        if not srcset_value:
            return srcset_value
        parts = srcset_value.split(',')
        processed_parts = []
        for part in parts:
            url_and_size = part.strip().split()
            if len(url_and_size) >= 1:
                url = url_and_size[0]
                if url.startswith(('http://', 'https://')):
                    new_url = f'/proxy?url={quote(url)}'
                else:
                    full_url = urljoin(base_url, url)
                    new_url = f'/proxy?url={quote(full_url)}'
                processed_parts.append(f'{new_url} {" ".join(url_and_size[1:])}')
        return ', '.join(processed_parts)

    # style属性内のURL書き換え
    def process_style_urls(style_content):
        if not style_content:
            return style_content
        url_pattern = r'url\(["\']?((?!data:)[^)"\']+)["\']?\)'
        def replace_url(match):
            url = match.group(1)
            if url.startswith(('http://', 'https://')):
                return f'url(/proxy?url={quote(url)})'
            full_url = urljoin(base_url, url)
            return f'url(/proxy?url={quote(full_url)})'
        return re.sub(url_pattern, replace_url, style_content)

    # メタリフレッシュタグの処理
    meta_refresh = soup.find('meta', {'http-equiv': lambda x: x and x.lower() == 'refresh'})
    if meta_refresh:
        content = meta_refresh.get('content', '')
        if 'url=' in content.lower():
            url_part = content.split('=', 1)[1]
            if url_part.startswith(('http://', 'https://')):
                new_content = content.replace(url_part, f'/proxy?url={quote(url_part)}')
            else:
                full_url = urljoin(base_url, url_part)
                new_content = content.replace(url_part, f'/proxy?url={quote(full_url)}')
            meta_refresh['content'] = new_content

    # baseタグの処理
    base_tag = soup.find('base')
    if base_tag:
        href = base_tag.get('href')
        if href:
            base_url = urljoin(base_url, href)
            base_tag['href'] = f'/proxy?url={quote(base_url)}'

    # 全てのタグを処理
    for tag_name, attrs in url_attributes.items():
        for tag in soup.find_all(tag_name):
            for attr in attrs:
                url = tag.get(attr)
                if url:
                    # srcset属性の特別処理
                    if attr == 'srcset':
                        tag[attr] = process_srcset(url)
                    # metaタグのURLを含むcontent属性の処理
                    elif tag_name == 'meta' and attr == 'content' and tag.get('property') in ['og:image', 'og:url', 'og:video']:
                        if url.startswith(('http://', 'https://')):
                            tag[attr] = f'/proxy?url={quote(url)}'
                        else:
                            full_url = urljoin(base_url, url)
                            tag[attr] = f'/proxy?url={quote(full_url)}'
                    # 通常のURL属性の処理
                    elif not url.startswith(('data:', 'mailto:', 'tel:', 'javascript:', '#', '//')):
                        if url.startswith(('http://', 'https://')):
                            tag[attr] = f'/proxy?url={quote(url)}'
                        else:
                            full_url = urljoin(base_url, url)
                            tag[attr] = f'/proxy?url={quote(full_url)}'

    # style属性内のURLを処理
    for tag in soup.find_all(style=True):
        tag['style'] = process_style_urls(tag['style'])

    # styleタグ内のCSSを処理
    for style in soup.find_all('style'):
        if style.string:
            style.string = process_css_content(style.string, base_url)

    # base URLを相対パスの解決に使用するためのJavaScriptを追加
    script_tag = soup.new_tag('script')
    script_tag.string = f'window.proxyBaseUrl = "{base_url}";'
    if soup.head:
        soup.head.insert(0, script_tag)
    else:
        soup.body.insert(0, script_tag)

    return str(soup)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/proxy', methods=['GET', 'POST'])
def proxy():
    url = request.args.get('url')
    if not url:
        return render_template('index.html', error='URLが指定されていません')

    try:
        # セッション管理
        clean_old_sessions()
        session_id = get_or_create_session()

        # URLの検証
        parsed_url = urlparse(url)
        if not parsed_url.scheme or not parsed_url.netloc:
            return render_template('index.html', error='無効なURLです')

        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
        if parsed_url.path:
            base_url = urljoin(base_url, os.path.dirname(parsed_url.path))

        # プロキシリクエストの作成
        method = request.method
        headers = {
            key: value for key, value in request.headers.items()
            if key.lower() not in ['host', 'content-length']
        }
        
        # User-Agentの設定
        chrome_version = "112.0.0.0"
        headers.update({
            'User-Agent': f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version} Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'Sec-Ch-Ua': f'"Not.A/Brand";v="8", "Chromium";v="{chrome_version}", "Google Chrome";v="{chrome_version}"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Upgrade-Insecure-Requests': '1',
            'Connection': 'keep-alive'
        })

        # Xboxドメイン特有の設定
        if 'xbox.com' in parsed_url.netloc:
            headers.update({
                'Origin': 'https://www.xbox.com',
                'Referer': 'https://www.xbox.com/',
                'authority': 'www.xbox.com',
                'dnt': '1',
                'x-forward-for': request.remote_addr,
                'x-requested-with': 'XMLHttpRequest',
                'x-ms-api-version': '2.0',
                'x-ms-client-request-id': str(uuid.uuid4()),
                'x-ms-correlation-id': str(uuid.uuid4()),
                'x-xbox-isautomated': 'false',
                'x-xbox-contract-version': '1',
                'x-client-CPU': 'x86',
                'x-client-SKU': 'web',
                'accept-language': 'ja-JP'
            })

            # 地域とロケール設定
            headers['accept-language'] = 'ja-JP'
            if '/ja-JP/' in url:
                headers['x-xbox-locale'] = 'ja-JP'
                headers['x-xbox-region'] = 'JP'

        # セッションのクッキーを追加
        if session_id in sessions:
            cookie_strings = []
            for name, cookie_data in sessions[session_id]['cookies'].items():
                if cookie_data['domain'] and parsed_url.netloc.endswith(cookie_data['domain'].lstrip('.')):
                    cookie_strings.append(f"{name}={cookie_data['value']}")
            if cookie_strings:
                headers['Cookie'] = '; '.join(cookie_strings)

        # セッション作成とリクエスト実行
        req_session = requests.Session()
        
        # プロキシパラメータの処理
        params = {}
        for key, value in request.args.items():
            if key != 'url':
                params[key] = value

        # URLクエリパラメータの結合
        url_parts = list(urlparse(url))
        url_query = parse_qs(url_parts[4])
        url_query.update(params)
        url_parts[4] = urlencode(url_query, doseq=True)

        # Xbox Cloud Gamingのパス処理
        if '/play' in url:
            url = urljoin(base_url, 'ja-JP/play')

        # リクエストの実行
        response = req_session.request(
            method=method,
            url=url,
            headers=headers,
            data=request.get_data() if method == 'POST' else None,
            allow_redirects=False,  # リダイレクトは手動で処理
            timeout=30,
            verify=True
        )

        # リダイレクト処理
        max_redirects = 10
        redirect_count = 0
        while response.status_code in [301, 302, 303, 307, 308] and redirect_count < max_redirects:
            location = response.headers.get('Location')
            if not location:
                break

            # リダイレクト先URLの処理
            if not location.startswith(('http://', 'https://')):
                location = urljoin(url, location)

            # セッションの更新
            update_session_data(session_id, response)

            # Xbox Cloud Gamingのパス処理
            if '/play' in location:
                location = urljoin(base_url, 'ja-JP/play')

            # リダイレクト先へのリクエスト
            response = req_session.request(
                method=method if response.status_code in [307, 308] else 'GET',
                url=location,
                headers=headers,
                allow_redirects=False,
                timeout=30,
                verify=True
            )
            redirect_count += 1

        # レスポンスヘッダーの準備
        response_headers = {}
        for key, value in response.headers.items():
            if key.lower() not in ['content-encoding', 'transfer-encoding', 'content-length', 'connection', 'set-cookie']:
                response_headers[key] = value

        # Content-Typeの処理
        content_type = response.headers.get('content-type', '').lower()
        if not content_type:
            content_type, _ = mimetypes.guess_type(url)
            if not content_type:
                content_type = 'application/octet-stream'
        
        response_headers['Content-Type'] = content_type

        # エラー応答の処理
        if response.status_code == 404 and 'xbox.com' in parsed_url.netloc:
            return redirect('/proxy?url=https://www.xbox.com/ja-JP/play', code=302)

        # HTMLコンテンツの場合
        if 'text/html' in content_type:
            encoding = detect_encoding(response)
            try:
                content = response.content.decode(encoding, errors='replace')
                content = modify_html_content(content, url)
                
                # レスポンスの作成
                proxy_response = make_response(Response(
                    content,
                    status=response.status_code,
                    headers=response_headers
                ))

                # セッションの更新とクッキーの転送
                update_session_data(session_id, response)
                for cookie in response.cookies:
                    cookie_domain = cookie.domain if cookie.domain else parsed_url.netloc
                    proxy_response.set_cookie(
                        cookie.name,
                        cookie.value,
                        expires=cookie.expires,
                        path=cookie.path,
                        domain=cookie_domain,
                        secure=cookie.secure,
                        httponly=cookie.has_nonstandard_attr('HttpOnly'),
                        samesite=cookie.get_nonstandard_attr('SameSite', 'Lax')
                    )

                return proxy_response

            except Exception as e:
                return render_template('index.html', error=f'エンコーディングエラー: {str(e)}')

        # CSS/JavaScriptの場合
        elif any(type_match in content_type for type_match in ['text/css', 'javascript', 'json']):
            try:
                encoding = detect_encoding(response)
                content = response.content.decode(encoding, errors='replace')
                
                # CSSファイル内のURLを処理
                if 'text/css' in content_type:
                    content = process_css_content(content, url)
                
                response_headers['Content-Type'] = f'{content_type}; charset=utf-8'
                proxy_response = make_response(Response(
                    content,
                    status=response.status_code,
                    headers=response_headers
                ))
                
                # セッションの更新
                update_session_data(session_id, response)
                return proxy_response

            except Exception as e:
                return Response(
                    response.content,
                    status=response.status_code,
                    headers=response_headers
                )

        # その他のコンテンツタイプの場合
        proxy_response = make_response(Response(
            response.content,
            status=response.status_code,
            headers=response_headers
        ))
        
        # セッションの更新
        update_session_data(session_id, response)
        return proxy_response

    except requests.RequestException as e:
        return render_template('index.html', error=f'エラーが発生しました: {str(e)}')
    except Exception as e:
        return render_template('index.html', error=f'予期せぬエラーが発生しました: {str(e)}')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(debug=True, host='0.0.0.0', port=port, threaded=True)