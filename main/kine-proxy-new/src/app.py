from flask import request, render_template, Response, abort, make_response, redirect
import flask
from urllib.parse import urlparse, urljoin, quote, unquote, parse_qsl, urlencode
import requests
import json
import os
import re
import chardet
from bs4 import BeautifulSoup
import mimetypes
import logging

# ロギング設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = flask.Flask(__name__, template_folder='../templates', static_folder='../static')

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
    """強化されたエンコーディング検出機能"""
    # 優先順位付きエンコーディング検出

    # 1. 明示的なエンコーディング情報を確認
    encodings_to_try = []
    
    # Content-Typeヘッダーからエンコーディングを取得
    content_type = response.headers.get('content-type', '').lower()
    charset_match = re.search(r'charset=([^\s;]+)', content_type)
    if charset_match:
        encoding = charset_match.group(1).strip('"\'')
        encodings_to_try.append(encoding)
    
    # 特定のサイトドメインに基づくエンコーディング優先順位
    url_domain = urlparse(response.url).netloc.lower()
    domain_encoding_map = {
        'google': ['utf-8'],
        'yahoo': ['utf-8', 'shift_jis'],
        'rakuten': ['euc-jp', 'utf-8'],
        'goo.ne.jp': ['utf-8', 'shift_jis'],
        'livedoor': ['utf-8'],
        'ameblo': ['utf-8'],
        'fc2': ['utf-8', 'shift_jis'],
        '2ch': ['shift_jis', 'euc-jp', 'utf-8'],
        '5ch': ['shift_jis', 'euc-jp', 'utf-8'],
    }
    
    for domain, encodings in domain_encoding_map.items():
        if domain in url_domain:
            for enc in encodings:
                if enc not in encodings_to_try:
                    encodings_to_try.append(enc)
    
    # 2. HTMLメタタグからエンコーディングを取得
    if 'text/html' in content_type and response.content:
        try:
            # 一般的なエンコーディングで試行
            for enc in ['utf-8', 'shift_jis', 'euc-jp']:
                try:
                    content_sample = response.content[:10000]  # 先頭部分のみ解析
                    soup = BeautifulSoup(content_sample, 'html.parser', from_encoding=enc)
                    
                    # <meta charset="..."> の形式
                    meta_charset = soup.find('meta', charset=True)
                    if meta_charset:
                        encoding = meta_charset.get('charset')
                        if encoding and encoding not in encodings_to_try:
                            encodings_to_try.append(encoding)
                    
                    # <meta http-equiv="Content-Type" content="text/html; charset=..."> の形式
                    meta_content_type = soup.find('meta', {'http-equiv': lambda x: x and x.lower() == 'content-type'})
                    if meta_content_type:
                        charset_match = re.search(r'charset=([^\s;]+)', meta_content_type.get('content', '').lower())
                        if charset_match:
                            encoding = charset_match.group(1).strip('"\'')
                            if encoding and encoding not in encodings_to_try:
                                encodings_to_try.append(encoding)
                                
                    # HTMLの解析に成功したらループを抜ける
                    break
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"HTML解析エラー: {str(e)}")
    
    # 3. 一般的な日本語サイトのエンコーディングを追加
    common_jp_encodings = ['utf-8', 'shift_jis', 'euc-jp', 'iso-2022-jp', 'cp932']
    for enc in common_jp_encodings:
        if enc not in encodings_to_try:
            encodings_to_try.append(enc)
    
    # 4. chardetでの検出結果を追加
    if response.content:
        try:
            sample = response.content[:8000]
            detected = chardet.detect(sample)
            if detected and detected['confidence'] > 0.5:
                encoding = detected['encoding']
                if encoding and encoding.lower() not in [e.lower() for e in encodings_to_try]:
                    encodings_to_try.append(encoding)
        except Exception as e:
            logger.warning(f"chardet検出エラー: {str(e)}")
    
    # 5. requestsのapparent_encodingを追加
    if hasattr(response, 'apparent_encoding') and response.apparent_encoding:
        if response.apparent_encoding not in encodings_to_try:
            encodings_to_try.append(response.apparent_encoding)
    
    # 6. その他の一般的なエンコーディングを追加
    additional_encodings = ['latin1', 'utf-16', 'windows-1251', 'windows-1252']
    for enc in additional_encodings:
        if enc not in encodings_to_try:
            encodings_to_try.append(enc)
    
    # 各エンコーディングを試行
    for encoding in encodings_to_try:
        try:
            # エンコーディング名の正規化
            encoding = encoding.lower().replace('-', '').replace('_', '')
            if encoding == 'shiftjis':
                encoding = 'shift_jis'
            elif encoding == 'utf8':
                encoding = 'utf-8'
            elif encoding == 'eucjp':
                encoding = 'euc-jp'
            
            # 最大エラー回避：デコードできない場合は'replace'で文字を置換
            sample = response.content[:4000]
            sample.decode(encoding, errors='strict')
            logger.info(f"使用するエンコーディング: {encoding}")
            return encoding
        except (LookupError, UnicodeDecodeError):
            continue
    
    # すべてのエンコーディングが失敗した場合のフォールバック
    logger.info("フォールバックエンコーディングとしてUTF-8を使用")
    return 'utf-8'

def modify_html_content(content, base_url, target_url):
    """HTMLコンテンツを修正し、すべてのリンクをプロキシ経由にする"""
    soup = BeautifulSoup(content, 'html.parser')
    
    # 処理対象のタグと属性
    url_attributes = {
        'a': ['href'],
        'img': ['src', 'srcset', 'data-src'],  # data-src追加（遅延読み込み対応）
        'link': ['href'],
        'script': ['src'],
        'iframe': ['src'],
        'form': ['action'],
        'meta': ['content'],  # Open Graph URLなど
        'video': ['src', 'poster'],
        'source': ['src', 'srcset'],
        'object': ['data'],
        'embed': ['src'],
        'audio': ['src'],
        'input': ['src'],  # input type="image"などの対応
        'track': ['src'],  # 字幕トラック対応
        'area': ['href'],   # 画像マップ対応
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

    # Google検索用の特別処理
    if 'google' in urlparse(target_url).netloc:
        # Google検索フォームの処理
        for form in soup.find_all('form'):
            if form.get('action'):
                # Google検索フォーム特有のパラメータを保持
                if 'search' in form.get('action').lower() or 'google' in form.get('action').lower():
                    form['method'] = 'GET'  # GETメソッドに強制
                    # hiddenフィールドを追加して元のGoogleURLを保持
                    hidden_input = soup.new_tag('input')
                    hidden_input['type'] = 'hidden'
                    hidden_input['name'] = 'original_action'
                    hidden_input['value'] = form['action']
                    form.append(hidden_input)
                    form['action'] = '/proxy'
        
        # Google検索結果のリンク処理（特別対応）
        for a in soup.find_all('a', href=True):
            href = a['href']
            # Googleの検索結果リンク（通常は/url?q=...の形式）
            if href.startswith('/url') and 'url?q=' in href or 'url?sa=' in href:
                parsed = urlparse(href)
                query_params = dict(parse_qsl(parsed.query))
                if 'q' in query_params:
                    real_url = query_params['q']
                    a['href'] = f'/proxy?url={quote(real_url)}'
            elif href.startswith('/search'):
                # 検索ページ内部リンク
                full_url = urljoin(base_url, href)
                a['href'] = f'/proxy?url={quote(full_url)}'

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

    # エンコーディング表示用のツールバーを追加（デバッグ目的）
    if app.debug:
        encoding_toolbar = soup.new_tag('div')
        encoding_toolbar['style'] = 'position:fixed;bottom:0;left:0;background:#f0f0f0;padding:5px;z-index:9999;font-size:12px;'
        encoding_toolbar.string = f"プロキシURL: {target_url}"
        if soup.body:
            soup.body.append(encoding_toolbar)

    # JavaScript用のメタ情報を追加
    script_tag = soup.new_tag('script')
    script_tag.string = f'window.proxyBaseUrl = "{original_base_url}";'
    if soup.head:
        soup.head.insert(0, script_tag)
    elif soup.body:
        soup.body.insert(0, script_tag)

    # エンコーディング指定を強制的にUTF-8に
    meta_charset = soup.find('meta', charset=True)
    if meta_charset:
        meta_charset['charset'] = 'utf-8'
    else:
        meta_charset = soup.new_tag('meta')
        meta_charset['charset'] = 'utf-8'
        if soup.head:
            soup.head.insert(0, meta_charset)
        
    meta_content_type = soup.find('meta', {'http-equiv': lambda x: x and x.lower() == 'content-type'})
    if meta_content_type:
        meta_content_type['content'] = 'text/html; charset=utf-8'
    
    return str(soup)

def process_js_content(js_content, base_url):
    """JavaScriptファイル内のURLをプロキシ経由に変換する"""
    if not js_content:
        return js_content
    
    # APIエンドポイントのURLを修正
    api_pattern = r'(["\'](https?://[^"\']+/(?:api|service|gateway|rest)/[^"\']+)["\'])'
    def replace_api(match):
        api_url = match.group(2)
        return f'"/proxy?url={quote(api_url)}"'
    js_content = re.sub(api_pattern, replace_api, js_content)
    
    # fetch/XMLHttpRequestで使われる完全なURLをプロキシ経由に変換
    ajax_pattern = r'((?:fetch|open)\s*\(\s*["\'])(https?://[^"\']+)(["\'])'
    def replace_ajax(match):
        ajax_url = match.group(2)
        return f'{match.group(1)}/proxy?url={quote(ajax_url)}{match.group(3)}'
    js_content = re.sub(ajax_pattern, replace_ajax, js_content)
    
    return js_content

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/proxy', methods=['GET', 'POST'])
def proxy():
    url = request.args.get('url')
    
    # Google検索フォームからの送信を処理
    if not url and 'original_action' in request.args:
        original_action = request.args.get('original_action')
        # Googleの場合、クエリパラメータを元のURLに追加
        if 'google' in original_action:
            search_query = request.args.get('q', '')
            if original_action.startswith('http'):
                google_url = original_action
            else:
                google_url = 'https://www.google.com/search'
            
            # Googleの検索URLを構築
            params = {}
            for key, value in request.args.items():
                if key not in ['original_action']:
                    params[key] = value
            
            # URLにパラメータを追加
            if '?' in google_url:
                full_url = f"{google_url}&{urlencode(params)}"
            else:
                full_url = f"{google_url}?{urlencode(params)}"
            
            # リダイレクト
            return redirect(f'/proxy?url={quote(full_url)}')
    
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
        
        # リクエストヘッダーの最適化
        headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'Sec-Ch-Ua': '"Not.A/Brand";v="8", "Chromium";v="114", "Google Chrome";v="114"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-User': '?1',
            'Sec-Fetch-Dest': 'document',
            'Dnt': '1',
        })

        # サイト別の特別設定
        hostname = parsed_url.netloc.lower()
        
        # Googleの場合の特別対応
        if 'google' in hostname:
            headers['Origin'] = 'https://www.google.com'
            headers['Referer'] = 'https://www.google.com/'
        # Yahoo Japanの場合の特別対応
        elif 'yahoo.co.jp' in hostname:
            headers['Origin'] = 'https://www.yahoo.co.jp'
            headers['Referer'] = 'https://www.yahoo.co.jp/'
        # 楽天の場合の特別対応
        elif 'rakuten' in hostname:
            headers['Origin'] = 'https://www.rakuten.co.jp'
            headers['Referer'] = 'https://www.rakuten.co.jp/'

        # リクエストのセッション作成
        session = requests.Session()
        
        # リクエスト実行
        try:
            response = session.request(
                method=method,
                url=url,
                headers=headers,
                data=request.get_data() if method == 'POST' else None,
                allow_redirects=True,
                timeout=30,
                verify=True
            )
        except requests.exceptions.SSLError:
            # SSL証明書エラーの場合は検証をスキップして再試行
            logger.warning(f"SSL証明書エラー、検証なしで再試行: {url}")
            response = session.request(
                method=method,
                url=url,
                headers=headers,
                data=request.get_data() if method == 'POST' else None,
                allow_redirects=True,
                timeout=30,
                verify=False
            )

        # 最終的なURLを取得（リダイレクト追跡後）
        final_url = response.url
        
        # リダイレクト時の元URLを保存（base_url計算に使用）
        if response.history:
            logger.info(f"リダイレクト: {url} → {final_url}")
            original_url = url
            url = final_url
            parsed_url = urlparse(url)
            base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
            if parsed_url.path:
                base_url = urljoin(base_url, os.path.dirname(parsed_url.path))

        # レスポンスヘッダーの準備
        response_headers = {}
        for key, value in response.headers.items():
            if key.lower() not in ['content-encoding', 'transfer-encoding', 'content-length', 'connection']:
                response_headers[key] = value
        
        # エンコーディング情報を強制的に設定
        content_type = response.headers.get('content-type', '').lower()
        if not content_type:
            content_type, _ = mimetypes.guess_type(url)
            if not content_type:
                content_type = 'application/octet-stream'
                
        response_headers['Content-Type'] = content_type

        # HTMLの処理
        if 'text/html' in content_type:
            # エンコーディングを検出
            encoding = detect_encoding(response)
            try:
                # HTML内容をデコード
                content = response.content.decode(encoding, errors='replace')
                # HTML内容を修正（リンクのプロキシ化など）
                content = modify_html_content(content, url, url)
                
                # コンテンツタイプを強制的にUTF-8に設定
                if 'content-type' in response_headers:
                    content_type_base = response_headers['Content-Type'].split(';')[0]
                    response_headers['Content-Type'] = f"{content_type_base}; charset=utf-8"
                
                # レスポンスの作成
                proxy_response = make_response(Response(
                    content,
                    status=response.status_code,
                    headers=response_headers
                ))
                
                # クッキーの処理
                for cookie in response.cookies:
                    cookie_options = {
                        'key': cookie.name,
                        'value': cookie.value,
                        'path': cookie.path or '/',
                        'secure': cookie.secure,
                        'httponly': cookie.has_nonstandard_attr('HttpOnly')
                    }
                    
                    # expires属性がある場合のみ設定
                    if cookie.expires:
                        cookie_options['expires'] = cookie.expires
                    
                    # localhostでは特別処理
                    if request.host not in ['localhost', '127.0.0.1'] and '.' in request.host:
                        cookie_options['domain'] = request.host
                    
                    proxy_response.set_cookie(**cookie_options)
                
                return proxy_response
                
            except Exception as e:
                logger.error(f"HTML処理エラー: {str(e)}")
                return render_template('index.html', error=f'HTML処理エラー: {str(e)}')

        # CSS/JavaScriptの処理
        elif any(type_match in content_type for type_match in ['text/css', 'javascript', 'json']):
            try:
                encoding = detect_encoding(response)
                content = response.content.decode(encoding, errors='replace')
                
                # CSSファイル内のURLを処理
                if 'text/css' in content_type:
                    content = process_css_content(content, url)
                # JavaScriptの処理
                elif 'javascript' in content_type:
                    content = process_js_content(content, url)
                
                # UTF-8に統一
                if 'content-type' in response_headers:
                    content_type_base = response_headers['content-type'].split(';')[0]
                    response_headers['content-type'] = f"{content_type_base}; charset=utf-8"
                
                return Response(content, status=response.status_code, headers=response_headers)
                
            except Exception as e:
                logger.warning(f"テキストデコードエラー: {str(e)} - バイナリとして応答")
                return Response(response.content, status=response.status_code, headers=response_headers)
        
        # バイナリコンテンツ（画像など）
        else:
            return Response(response.content, status=response.status_code, headers=response_headers)

    except requests.RequestException as e:
        logger.error(f"リクエストエラー: {str(e)}")
        return render_template('index.html', error=f'リクエストエラー: {str(e)}')
    except Exception as e:
        logger.error(f"予期せぬエラー: {str(e)}")
        return render_template('index.html', error=f'予期せぬエラーが発生しました: {str(e)}')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(debug=True, host='0.0.0.0', port=port, threaded=True)