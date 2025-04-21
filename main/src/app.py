from flask import Flask, request, render_template, Response, abort, make_response
import requests
from urllib.parse import urlparse, urljoin, quote, unquote
import json
import os
import re
import chardet
from bs4 import BeautifulSoup
import mimetypes

app = Flask(__name__, template_folder='../templates', static_folder='../static')

def process_css_content(css_content, base_url):
    """CSSファイル内のURLをプロキシ経由に変換する"""
    if not css_content:
        return css_content
    
    # CSSの@importルールを処理
    import_pattern = r'@import\s+[\'"]([^\'"]+)[\'"]'
    def replace_import(match):
        url = match.group(1)
        if url.startswith(('http://', 'https://')):
            return f'@import "/proxy?url={quote(url)}"'
        full_url = urljoin(base_url, url)
        return f'@import "/proxy?url={quote(full_url)}"'
    css_content = re.sub(import_pattern, replace_import, css_content)
    
    # 通常のURLを処理
    url_pattern = r'url\(["\']?((?!data:)[^)"\']+)["\']?\)'
    def replace_url(match):
        url = match.group(1)
        if url.startswith(('http://', 'https://')):
            return f'url(/proxy?url={quote(url)})'
        elif url.startswith('//'):
            # プロトコル相対URLの処理
            parsed_base = urlparse(base_url)
            full_url = f"{parsed_base.scheme}:{url}"
            return f'url(/proxy?url={quote(full_url)})'
        full_url = urljoin(base_url, url)
        return f'url(/proxy?url={quote(full_url)})'
    
    return re.sub(url_pattern, replace_url, css_content)

def detect_encoding(response):
    """コンテンツのエンコーディングを検出する(改善版)"""
    # 1. Content-Typeヘッダーからエンコーディングを取得
    content_type = response.headers.get('content-type', '').lower()
    charset_match = re.search(r'charset=([^\s;]+)', content_type)
    if charset_match:
        encoding = charset_match.group(1).strip('"\'')
        try:
            # 検証: 実際にデコードしてみる
            sample = response.content[:4000]  # 先頭部分だけ試す
            sample.decode(encoding, errors='strict')
            return encoding
        except (LookupError, UnicodeDecodeError):
            # 無効なエンコーディング名か、デコードできない場合は次へ
            pass

    # 2. HTMLメタタグからエンコーディングを取得
    if 'text/html' in content_type:
        try:
            # まずUTF-8で試してみる（一般的なケース）
            content_sample = response.content[:10000]  # 先頭部分のみ解析
            soup = BeautifulSoup(content_sample, 'html.parser', from_encoding='utf-8')
            
            # <meta charset="..."> の形式
            meta_charset = soup.find('meta', charset=True)
            if meta_charset:
                encoding = meta_charset.get('charset')
                try:
                    sample = response.content[:4000]
                    sample.decode(encoding, errors='strict')
                    return encoding
                except (LookupError, UnicodeDecodeError):
                    pass
            
            # <meta http-equiv="Content-Type" content="text/html; charset=..."> の形式
            meta_content_type = soup.find('meta', {'http-equiv': lambda x: x and x.lower() == 'content-type'})
            if meta_content_type:
                charset_match = re.search(r'charset=([^\s;]+)', meta_content_type.get('content', '').lower())
                if charset_match:
                    encoding = charset_match.group(1).strip('"\'')
                    try:
                        sample = response.content[:4000]
                        sample.decode(encoding, errors='strict')
                        return encoding
                    except (LookupError, UnicodeDecodeError):
                        pass
        except:
            pass

    # 3. chardetを使用してエンコーディングを推測
    try:
        if not response.content:
            return 'utf-8'
        
        # 最初の数KBだけ使用してエンコーディング検出
        sample = response.content[:4000]
        detected = chardet.detect(sample)
        if detected and detected['confidence'] > 0.7:
            encoding = detected['encoding']
            if encoding:
                try:
                    sample.decode(encoding, errors='strict')
                    return encoding
                except (LookupError, UnicodeDecodeError):
                    pass
    except:
        pass

    # コンテンツタイプに基づく推測
    if 'text/html' in content_type:
        if hasattr(response, 'apparent_encoding') and response.apparent_encoding:
            return response.apparent_encoding
    
    # 4. 一般的なエンコーディングを順に試す
    encodings_to_try = ['utf-8', 'shift_jis', 'euc-jp', 'iso-2022-jp', 'cp932', 'latin1']
    for encoding in encodings_to_try:
        try:
            sample = response.content[:4000]
            sample.decode(encoding, errors='strict')
            return encoding
        except (LookupError, UnicodeDecodeError):
            continue

    # 5. デフォルトのエンコーディング
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
        'audio': ['src'],  # オーディオタグ追加
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
                elif url.startswith('//'):
                    # プロトコル相対URLの処理
                    parsed_base = urlparse(base_url)
                    full_url = f"{parsed_base.scheme}:{url}"
                    new_url = f'/proxy?url={quote(full_url)}'
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
            elif url.startswith('//'):
                parsed_base = urlparse(base_url)
                full_url = f"{parsed_base.scheme}:{url}"
                return f'url(/proxy?url={quote(full_url)})'
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
            elif url_part.startswith('//'):
                parsed_base = urlparse(base_url)
                full_url = f"{parsed_base.scheme}:{url_part}"
                new_content = content.replace(url_part, f'/proxy?url={quote(full_url)}')
            else:
                full_url = urljoin(base_url, url_part)
                new_content = content.replace(url_part, f'/proxy?url={quote(full_url)}')
            meta_refresh['content'] = new_content

    # baseタグの処理
    base_tag = soup.find('base')
    original_base_url = base_url
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
                        elif url.startswith('//'):
                            parsed_base = urlparse(base_url)
                            full_url = f"{parsed_base.scheme}:{url}"
                            tag[attr] = f'/proxy?url={quote(full_url)}'
                        else:
                            full_url = urljoin(base_url, url)
                            tag[attr] = f'/proxy?url={quote(full_url)}'
                    # 通常のURL属性の処理
                    elif not url.startswith(('data:', 'mailto:', 'tel:', 'javascript:', '#')):
                        if url.startswith(('http://', 'https://')):
                            tag[attr] = f'/proxy?url={quote(url)}'
                        elif url.startswith('//'):
                            parsed_base = urlparse(base_url)
                            full_url = f"{parsed_base.scheme}:{url}"
                            tag[attr] = f'/proxy?url={quote(full_url)}'
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
    script_tag.string = f'window.proxyBaseUrl = "{original_base_url}";'
    if soup.head:
        soup.head.insert(0, script_tag)
    elif soup.body:
        soup.body.insert(0, script_tag)
    else:
        soup.append(script_tag)

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
        headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',  # brotliサポート追加
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'Sec-Ch-Ua': '"Chromium";v="112", "Google Chrome";v="112", "Not:A-Brand";v="99"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-User': '?1',
            'Sec-Fetch-Dest': 'document'
        })

        # Xbox特有のヘッダー設定
        is_xbox = False
        if 'xbox.com' in parsed_url.netloc:
            is_xbox = True
            headers['Origin'] = 'https://www.xbox.com'
            headers['Referer'] = 'https://www.xbox.com/'
            # Xboxサイト専用のヘッダー追加
            headers['X-Requested-With'] = 'XMLHttpRequest'
            headers['DNT'] = '1'

        # クッキーの処理
        if 'Cookie' in request.headers:
            headers['Cookie'] = request.headers['Cookie']

        # セッション作成
        session = requests.Session()
        
        # リクエストの実行
        response = session.request(
            method=method,
            url=url,
            headers=headers,
            data=request.get_data() if method == 'POST' else None,
            allow_redirects=True,
            timeout=60,  # タイムアウト延長
            verify=True
        )

        # リダイレクト時の元URLを保存（CSSのbase_url計算に使用）
        if response.history:
            original_url = url
            url = response.url
            parsed_url = urlparse(url)
            base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
            if parsed_url.path:
                base_url = urljoin(base_url, os.path.dirname(parsed_url.path))

        # レスポンスヘッダーの準備
        response_headers = {}
        for key, value in response.headers.items():
            if key.lower() not in ['content-encoding', 'transfer-encoding', 'content-length', 'connection']:
                response_headers[key] = value

        # Content-Typeの処理
        content_type = response.headers.get('content-type', '').lower()
        if not content_type:
            content_type, _ = mimetypes.guess_type(url)
            if not content_type:
                content_type = 'application/octet-stream'
        
        response_headers['Content-Type'] = content_type

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

                # クッキーの転送（ローカルホスト対応）
                if 'Set-Cookie' in response.headers:
                    for cookie in response.cookies:
                        cookie_options = {
                            'key': cookie.name,
                            'value': cookie.value,
                            'expires': cookie.expires,
                            'path': cookie.path,
                            'secure': cookie.secure,
                            'httponly': cookie.has_nonstandard_attr('HttpOnly')
                        }
                        
                        # localhost/127.0.0.1 ではドメイン設定を省略する
                        if request.host not in ['localhost', '127.0.0.1'] and '.' in request.host:
                            cookie_options['domain'] = request.host
                        
                        proxy_response.set_cookie(**cookie_options)

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
                
                # XMLHttpRequestを使用するJavaScriptの処理 (Xbox固有の処理)
                if is_xbox and 'javascript' in content_type:
                    # APIエンドポイントのURLを修正
                    api_pattern = r'(["\'](https?://[^"\']+/api/[^"\']+)["\'])'
                    def replace_api(match):
                        api_url = match.group(2)
                        return f'"/proxy?url={quote(api_url)}"'
                    content = re.sub(api_pattern, replace_api, content)
                
                response_headers['Content-Type'] = f'{content_type}; charset=utf-8'
                return Response(
                    content,
                    status=response.status_code,
                    headers=response_headers
                )
            except Exception as e:
                # デコードエラーの場合はバイナリとして返す
                return Response(
                    response.content,
                    status=response.status_code,
                    headers=response_headers
                )

        # その他のコンテンツタイプの場合
        return Response(
            response.content,
            status=response.status_code,
            headers=response_headers
        )

    except requests.RequestException as e:
        return render_template('index.html', error=f'リクエストエラー: {str(e)}')
    except Exception as e:
        return render_template('index.html', error=f'予期せぬエラーが発生しました: {str(e)}')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(debug=True, host='0.0.0.0', port=port, threaded=True)