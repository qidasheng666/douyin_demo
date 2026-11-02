"""
    抖音数据采集工具 spider_tools.py

    本文件可以提取抖音视频标题描述、文案、下载视频、提取博主作品列表等功能。

    【主要功能】
    1. get_video_link(link)
       根据抖音视频公网链接（支持完整链接 / video/ 短链 / v.douyin.com 短链，
       短链会自动重定向解析），提取视频真实播放地址与视频标题。
       返回元组 (真实视频地址, 视频标题)。
       使用独立的无界面 Chrome 加载视频页面，监听浏览器请求获取详情。

    2. download_video(link, save_path="")
       调用 get_video_link 获取视频地址后，将视频下载并以标题命名保存为
       .mp4 文件到指定目录（默认当前目录）。

    3. get_video_text(link)
       调用 get_video_link 获取视频地址，再使用阿里云百炼平台的
       paraformer-v2 语音识别模型，将视频语音转为文字文案返回。
       （需配置环境变量 DASHSCOPE_API_KEY 或 dashscope.api_key）

    4. get_user_videos(user_url, count=10)
       根据抖音博主主页链接（支持 user/ 完整链接或 v.douyin.com 短链），
       通过浏览器滚动加载作品列表，提取指定数量（count）的视频公网链接并返回列表。

    【命令行用法】
        python spider_tools.py text <视频链接>            # 获取视频文案
        python spider_tools.py desc <视频链接>            # 获取视频标题
        python spider_tools.py download <视频链接> [目录]  # 下载视频
        python spider_tools.py videos <博主主页链接> [数量] # 提取博主视频链接

    【依赖】
        requests, dashscope, DrissionPage
        需要 Chrome/Chromium 浏览器；可用 DOUYIN_BROWSER_PATH 指定可执行文件。

    【注意事项】
    - 视频转文字可在项目 .env 中设置 DASHSCOPE_API_KEY，也支持环境变量或 dashscope.api_key。
      转写失败时会显示百炼返回的错误码和原因。
    - 默认使用临时浏览器配置；可通过 browser_profile 保存手动登录状态。
    - 作品列表要求登录或验证时，可设置 headless=False 和 login_timeout。
"""
import argparse
import json
import os
import re
import socket
import sys
import tempfile
import time
import uuid
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import dashscope
import requests
from dashscope import Transcription
from dashscope.api_entities.dashscope_response import TranscriptionResponse
from dashscope.utils.oss_utils import OssUtils
from DrissionPage import ChromiumPage, ChromiumOptions
from dotenv import load_dotenv


# 始终读取脚本所在目录的配置，兼容 IDE 或从其他目录启动；已有环境变量优先。
load_dotenv(Path(__file__).resolve().parent / '.env', override=False)


class VideoTranscriptionError(RuntimeError):
    """可供调用方判断和记录的转写错误；不包含 API Key 或源文件签名 URL。"""

    def __init__(self, message, *, stage, code, task_id=None, request_id=None,
                 task_status=None):
        self.stage = stage
        self.code = code
        self.message = message
        self.task_id = task_id
        self.request_id = request_id
        self.task_status = task_status
        super().__init__(
            f'{message} [stage={stage}, code={code}, task_id={task_id}, '
            f'request_id={request_id}, task_status={task_status}]')


class DouyinSpider:
    def __init__(self, headless=True, browser_profile=None):
        self.headless = headless
        # 可选独立配置目录，用于保存手动登录后的浏览器会话。
        self.browser_profile = str(Path(browser_profile).resolve()) if browser_profile else None
        self.headers = {
            'referer': 'https://www.douyin.com/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
        }
        self.timeout = (15, 60)

    def __open_browser(self, profile):
        # 独立端口和配置目录，避免连接或关闭用户日常使用的浏览器。
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        options = ChromiumOptions().set_local_port(port).headless(self.headless)
        options.set_user_data_path(self.browser_profile or profile)
        browser_path = os.environ.get('DOUYIN_BROWSER_PATH')
        mac_chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
        if browser_path:
            options.set_browser_path(browser_path)
        elif Path(mac_chrome).is_file():
            options.set_browser_path(mac_chrome)
        options.set_timeouts(base=15, page_load=30, script=15)
        return ChromiumPage(addr_or_opts=options)

    def __browser_video_detail(self, video_id):
        with tempfile.TemporaryDirectory(prefix='douyin-video-') as profile:
            page = self.__open_browser(profile)
            try:
                page.listen.start('aweme/v1/web/aweme/detail')
                page.get(f'https://www.douyin.com/video/{video_id}')
                # 一个页面可能请求推荐视频，只接受用户指定的视频详情。
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    packet = page.listen.wait(
                        timeout=max(1, deadline - time.monotonic()), raise_err=False)
                    if not packet:
                        break
                    body = packet.response.body
                    if isinstance(body, str):
                        try:
                            body = json.loads(body)
                        except ValueError:
                            continue
                    detail = body.get('aweme_detail') if isinstance(body, dict) else None
                    if detail and str(detail.get('aweme_id')) == video_id:
                        return detail
                raise RuntimeError('浏览器未取得视频详情，请检查视频是否可访问或页面是否要求登录/验证')
            finally:
                page.quit()

    @staticmethod
    def __video_id(link):
        match = re.search(r'https?://[^\s<>]+', link)
        url = match.group(0) if match else link.strip()
        parsed = urlsplit(url)
        match = re.search(r'/(?:video|note)/(\d+)(?:/|$)', parsed.path)
        if match:
            return match.group(1)
        for key in ('modal_id', 'aweme_id', 'item_id'):
            value = parse_qs(parsed.query).get(key, [''])[0]
            if value.isdigit():
                return value
        return None

    def __found_302(self, link):
        """重定向链接处理"""
        response = requests.get(link, headers={'user-agent': self.headers['user-agent']}, timeout=self.timeout)
        response.raise_for_status()
        return response.url

    def get_video_link(self, link):
        """
            提取视频链接
        :param link: 抖音视频公网链接
        :return: 元组类型，(真实视频地址,视频标题)
        """
        video_id = self.__video_id(link)
        if not video_id:
            match = re.search(r'https://v\.douyin\.com/[\w/-]+', link)
            if match:
                video_id = self.__video_id(self.__found_302(match.group(0)))
        if not video_id:
            raise ValueError('无法从链接中识别抖音视频 ID')

        try:
            detail = self.__browser_video_detail(video_id)
        except Exception as exc:
            raise RuntimeError(f'获取视频失败：{exc}') from exc

        video = detail.get('video') or {}
        for key in ('play_addr_h264', 'play_addr', 'download_addr'):
            urls = (video.get(key) or {}).get('url_list') or []
            urls = [url for url in urls if isinstance(url, str) and url.startswith(('https://', 'http://'))]
            if urls:
                title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', detail.get('desc') or '')
                # 限制 UTF-8 字节数，避免中文标题超过文件系统的文件名长度限制。
                title = title.encode('utf-8')[:180].decode('utf-8', errors='ignore').strip('. ')
                # 最后一项常是 www.douyin.com 的重定向入口，优先直接 CDN。
                direct = [url for url in urls
                          if (urlsplit(url).hostname or '').endswith('.douyinvod.com')]
                return (direct or urls)[0], title or video_id
        raise RuntimeError('视频详情没有可用的播放地址，可能是图文作品或不可下载的视频')

    def download_video(self, link, save_path="", *, quiet=False):
        """流式下载；校验状态和 MP4 文件头后，原子写入目标文件。"""
        video_link, title = self.get_video_link(link)
        if not quiet:
            print('真实链接：', video_link)
        directory = Path(save_path or '.')
        directory.mkdir(parents=True, exist_ok=True)
        video_name = directory / f'{title}.mp4'
        partial = None
        try:
            with requests.get(video_link, headers={
                'user-agent': self.headers['user-agent'],
                'referer': 'https://www.douyin.com/',
            }, timeout=self.timeout, stream=True) as response:
                response.raise_for_status()
                written = 0
                prefix = bytearray()
                with tempfile.NamedTemporaryFile(dir=directory, suffix='.part', delete=False) as output:
                    partial = Path(output.name)
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        if len(prefix) < 32:
                            prefix.extend(chunk[:32 - len(prefix)])
                        output.write(chunk)
                        written += len(chunk)
                if written < 12 or prefix[4:8] != b'ftyp':
                    raise RuntimeError('下载内容不是有效的 MP4 文件，未保存为视频')
                expected = response.headers.get('content-length')
                if expected and not response.headers.get('content-encoding') and written != int(expected):
                    raise RuntimeError('视频下载不完整，请重试')
            partial.replace(video_name)
            if not quiet:
                print(f'保存成功：{video_name.resolve()}（{written:,} 字节）')
            return video_name
        finally:
            if partial is not None:
                partial.unlink(missing_ok=True)

    @staticmethod
    def __retry_request(call, *args, **kwargs):
        """只重试连接/超时异常；任务查询重用原 task_id，不重复提交计费任务。"""
        for attempt in range(3):
            try:
                return call(*args, **kwargs)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)

    def __stage_video_for_transcription(self, link, api_key):
        """下载并上传百炼临时存储，避免百炼直接访问抖音被 403 拒绝。

        临时文件由百炼保留 48 小时；适用于脚本，生产环境应使用自有 OSS。
        """
        with tempfile.TemporaryDirectory(prefix='douyin-transcription-') as directory:
            try:
                video_path = self.download_video(link, directory, quiet=True)
            except (RuntimeError, ValueError, OSError, requests.RequestException) as exc:
                raise VideoTranscriptionError(
                    f'转存前下载视频失败：{type(exc).__name__}',
                    stage='source', code='LOCAL_DOWNLOAD_FAILED') from exc
            if video_path.stat().st_size > 1024 ** 3:
                raise VideoTranscriptionError(
                    '视频超过百炼临时存储的 1GB 上传限制',
                    stage='upload', code='UPLOAD_FILE_TOO_LARGE')
            try:
                policy = self.__retry_request(
                    OssUtils.get_upload_certificate, model='paraformer-v2', api_key=api_key)
                if policy.status_code != HTTPStatus.OK:
                    raise VideoTranscriptionError(
                        f'获取百炼上传凭证失败：{policy.message}', stage='upload',
                        code=policy.code or 'UPLOAD_POLICY_FAILED',
                        request_id=getattr(policy, 'request_id', None))
                info = policy.output
                key = info['upload_dir'] + '/' + uuid.uuid4().hex + '.mp4'
                form = {
                    'OSSAccessKeyId': info['oss_access_key_id'],
                    'Signature': info['signature'],
                    'policy': info['policy'],
                    'key': key,
                    'x-oss-object-acl': info['x_oss_object_acl'],
                    'x-oss-forbid-overwrite': info['x_oss_forbid_overwrite'],
                    'success_action_status': '200',
                }
                with video_path.open('rb') as video_file:
                    response = requests.post(
                        info['upload_host'], data=form,
                        files={'file': ('source.mp4', video_file, 'video/mp4')},
                        timeout=(15, 300))
                response.raise_for_status()
                return 'oss://' + key
            except requests.RequestException as exc:
                raise VideoTranscriptionError(
                    f'上传百炼临时存储失败：{type(exc).__name__}',
                    stage='upload', code='UPLOAD_FAILED') from exc

    def __transcribe_video_url(self, url, api_key):
        """提交一个转写任务，等待其结束并返回文件子任务结果。"""
        try:
            if url.startswith('oss://'):
                # Paraformer 临时 oss:// URL 使用 HTTP 接口及资源解析 Header。
                response = requests.post(
                    dashscope.base_http_api_url.rstrip('/') + '/services/audio/asr/transcription',
                    headers={'Authorization': f'Bearer {api_key}',
                             'X-DashScope-Async': 'enable',
                             'X-DashScope-OssResourceResolve': 'enable'},
                    json={'model': 'paraformer-v2', 'input': {'file_urls': [url]},
                          'parameters': {'language_hints': ['zh', 'en']}},
                    timeout=self.timeout)
                payload = response.json()
                task_response = TranscriptionResponse(
                    status_code=response.status_code,
                    request_id=payload.get('request_id'),
                    code=payload.get('code'), message=payload.get('message'),
                    output=payload.get('output'))
            else:
                task_response = Transcription.async_call(
                    model='paraformer-v2',
                    file_urls=[url],
                    language_hints=['zh', 'en'],
                    api_key=api_key,
                )
        except requests.RequestException as exc:
            # 提交时断线可能已创建任务，不自动重提，避免重复任务。
            raise VideoTranscriptionError(
                f'提交转写任务网络异常：{type(exc).__name__}；无法确认任务是否已创建',
                stage='submit', code='SUBMISSION_NETWORK_ERROR') from exc
        task_output = task_response.output or {}
        if task_response.status_code != HTTPStatus.OK or not task_output.get('task_id'):
            raise VideoTranscriptionError(
                f'提交转写任务失败：{task_response.code or "未知错误"}；'
                f'{task_response.message or task_output.get("message") or "未返回 task_id"}',
                stage='submit', code=task_response.code or 'MISSING_TASK_ID',
                task_id=task_output.get('task_id'),
                request_id=getattr(task_response, 'request_id', None))

        try:
            transcribe_response = self.__retry_request(
                Transcription.wait, task=task_output['task_id'], api_key=api_key,
                wait_timeout=300)
        except requests.RequestException as exc:
            raise VideoTranscriptionError(
                f'查询转写任务网络异常：{type(exc).__name__}；可使用 task_id 继续查询',
                stage='wait', code='QUERY_NETWORK_ERROR',
                task_id=task_output['task_id']) from exc
        output = transcribe_response.output or {}
        if transcribe_response.status_code != HTTPStatus.OK:
            raise VideoTranscriptionError(
                f'查询转写任务失败：{transcribe_response.code or "未知错误"}；'
                f'{transcribe_response.message or output.get("message") or "未返回错误详情"}',
                stage='wait', code=transcribe_response.code or 'QUERY_FAILED',
                task_id=task_output['task_id'],
                request_id=getattr(transcribe_response, 'request_id', None),
                task_status=output.get('task_status'))
        results = output.get('results') or []
        if not results:
            raise VideoTranscriptionError(
                f'转写任务未返回结果：task_status={output.get("task_status")}；'
                f'{output.get("message") or "请检查任务状态"}',
                stage='recognize', code=output.get('code') or 'EMPTY_RESULTS',
                task_id=task_output['task_id'],
                request_id=getattr(transcribe_response, 'request_id', None),
                task_status=output.get('task_status'))
        result = dict(results[0])
        result['task_id'] = task_output['task_id']
        result['request_id'] = getattr(transcribe_response, 'request_id', None)
        result['task_status'] = output.get('task_status')
        # 某些响应把错误详情放在任务层。
        for key in ('code', 'message'):
            if not result.get(key) and output.get(key):
                result[key] = output[key]
        return result

    def get_video_text(self, link):
        """
        返回视频语音转写文本；失败抛出 VideoTranscriptionError。

        link 为抖音作品链接，不是传给百炼的媒体文件 URL。
        源文件 403 时下载并上传百炼临时存储后重提一次；查询超时保留 task_id，
        可用 Transcription.wait(task=异常.task_id) 继续查询原任务。
        """
        if not isinstance(link, str) or not link.strip():
            raise VideoTranscriptionError(
                '请传入非空的抖音作品链接', stage='source', code='INVALID_VIDEO_LINK')
        api_key = os.environ.get('DASHSCOPE_API_KEY') or dashscope.api_key
        if not api_key:
            raise ValueError('请先在https://bailian.console.aliyun.com/cn-beijing/model/settings/api-key中申请api key，并在项目 .env 中填写 DASHSCOPE_API_KEY')
        # 最多两个任务：直接 CDN，及 403 后的百炼临时存储。
        for attempt in range(2):
            if attempt:
                url = self.__stage_video_for_transcription(link, api_key)
            else:
                url = self.__video_url_for_transcription(link)
            result = self.__transcribe_video_url(url, api_key)
            context = {key: result.get(key) for key in
                       ('task_id', 'request_id', 'task_status')}
            if result.get('subtask_status') == 'SUCCEEDED':
                break
            code = result.get('code') or '未知错误'
            message = result.get('message') or '未返回错误详情'
            if result.get('subtask_status') == 'FAILED' and code == 'FILE_403_FORBIDDEN' and not attempt:
                print('百炼下载抖音视频被拒绝，正在下载并转存到百炼临时存储后重试。', file=sys.stderr)
                continue
            raise VideoTranscriptionError(
                f'视频转写失败：subtask_status={result.get("subtask_status")}；'
                f'{code}；{message}', stage='recognize', code=code, **context)
        return self.__read_transcription(result, context)

    def __video_url_for_transcription(self, link):
        try:
            url = self.get_video_link(link)[0]
        except (ValueError, RuntimeError, requests.RequestException) as exc:
            raise VideoTranscriptionError(
                f'获取视频失败：{exc}', stage='source',
                code='VIDEO_SOURCE_UNAVAILABLE') from exc
        parsed_source = urlsplit(url) if isinstance(url, str) else None
        if not parsed_source or parsed_source.scheme not in ('http', 'https') or not parsed_source.netloc:
            raise VideoTranscriptionError(
                '视频播放地址不是有效的 HTTP(S) URL', stage='source',
                code='INVALID_MEDIA_URL')
        return url

    def __read_transcription(self, result, context):
        transcription_url = result.get('transcription_url')
        if not isinstance(transcription_url, str):
            raise VideoTranscriptionError(
                '转写任务成功但未返回有效的 transcription_url', stage='result',
                code='INVALID_RESULT_URL', **context)
        parsed = urlsplit(transcription_url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            raise VideoTranscriptionError(
                '转写任务返回的 transcription_url 不是有效的 HTTP(S) 地址',
                stage='result', code='INVALID_RESULT_URL', **context)
        try:
            response = self.__retry_request(requests.get, transcription_url, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise VideoTranscriptionError(
                f'下载转写结果失败：{type(exc).__name__}', stage='result',
                code='RESULT_DOWNLOAD_FAILED', **context) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise VideoTranscriptionError(
                '转写结果不是有效的 JSON', stage='result',
                code='INVALID_RESULT_JSON', **context) from exc
        if not isinstance(data, dict):
            raise VideoTranscriptionError(
                '转写结果不是 JSON 对象', stage='result',
                code='INVALID_RESULT_JSON', **context)
        transcripts = data.get('transcripts') or []
        if not isinstance(transcripts, list) or any(
                not isinstance(item, dict) or not isinstance(item.get('text', ''), str)
                for item in transcripts):
            raise VideoTranscriptionError(
                '转写结果的 transcripts 格式异常', stage='result',
                code='INVALID_TRANSCRIPTS', **context)
        text = ''.join(item.get('text', '') for item in transcripts).strip()
        if not text:
            raise VideoTranscriptionError(
                '转写结果中未识别到文案', stage='result', code='EMPTY_TRANSCRIPT',
                **context)
        return text

    def get_video_text2(self, link):
        """兼容旧调用；使用 get_video_text 的同一套转写逻辑。"""
        return self.get_video_text(link)

    def __get_sec_user_id(self, user_url):
        """从博主主页链接中提取 sec_user_id，支持 v.douyin.com 短链重定向"""
        if 'user/' in user_url:
            sec_user_id = user_url.split('user/')[-1].strip().split('?')[0]
        else:
            final_url = self.__found_302(user_url)
            sec_user_id = re.findall(r'user/([^/?]+)', final_url)[0]
        return sec_user_id

    @staticmethod
    def __scroll_user_works(page):
        """作品列表在独立容器内滚动，滚动窗口不会触发加载。"""
        page.run_js("""
            const container = document.querySelector('.route-scroll-container')
                || document.scrollingElement;
            container.scrollTo(0, container.scrollHeight);
        """)

    def get_user_videos(self, user_url, count=10, login_timeout=0):
        """通过浏览器分页获取作品链接，数量不足时以实际作品数为准。

        login_timeout 为等待手动登录/验证的秒数，需配合 headless=False。
        browser_profile 可保存登录状态，后续无需每次重新登录。
        """
        if count <= 0:
            return []
        if login_timeout < 0:
            raise ValueError('login_timeout 不能小于 0')
        if login_timeout and self.headless:
            raise ValueError('等待手动登录需设置 DouyinSpider(headless=False)')
        sec_user_id = self.__get_sec_user_id(user_url)
        video_links = []
        seen_ids = set()
        with tempfile.TemporaryDirectory(prefix='douyin-user-') as profile:
            page = self.__open_browser(profile)
            try:
                page.listen.start('aweme/v1/web/aweme/post/')
                page.get(f'https://www.douyin.com/user/{sec_user_id}')
                login_deadline = time.monotonic() + login_timeout
                retries = 0
                if login_timeout:
                    print('请在浏览器中完成登录或验证；若作品未自动加载，请手动刷新页面。', file=sys.stderr)
                while len(video_links) < count:
                    packet = page.listen.wait(timeout=10, raise_err=False)
                    body = packet.response.body if packet else None
                    if isinstance(body, str):
                        try:
                            body = json.loads(body)
                        except ValueError:
                            body = None
                    valid = (isinstance(body, dict)
                             and body.get('status_code', 0) == 0
                             and isinstance(body.get('aweme_list'), list))
                    if not valid:
                        retries += 1
                        if time.monotonic() < login_deadline:
                            # 留出交互时间，避免页面不断刷新打断扫码。
                            time.sleep(3)
                            self.__scroll_user_works(page)
                            continue
                        if retries <= 2:
                            if video_links:
                                self.__scroll_user_works(page)
                            else:
                                page.refresh()
                            continue
                        reason = '未监听到作品响应' if not packet else '作品接口返回空内容或异常数据'
                        raise RuntimeError(
                            f'{reason}，已获取 {len(video_links)} 条；这不代表博主没有作品。'
                            '请使用 DouyinSpider(headless=False, browser_profile=".douyin-browser")，'
                            '并为 get_user_videos 设置 login_timeout=180，完成登录/验证后重试。')
                    retries = 0
                    previous_count = len(video_links)
                    for aweme in body['aweme_list']:
                        aweme_id = aweme.get('aweme_id')
                        if aweme_id and aweme_id not in seen_ids:
                            seen_ids.add(aweme_id)
                            video_links.append(f'https://www.douyin.com/video/{aweme_id}')
                            if len(video_links) >= count:
                                break
                    if len(video_links) >= count or not body.get('has_more'):
                        break
                    if len(video_links) == previous_count:
                        raise RuntimeError('作品接口重复返回相同数据，分页没有进展')
                    self.__scroll_user_works(page)
            finally:
                page.quit()
        return video_links


# # 测试代码
# ds = DouyinSpider(headless=True, browser_profile=".douyin-browser")
# # ds.download_video("https://www.douyin.com/video/7660888653740166415")
# # print(ds.get_video_text("https://www.douyin.com/video/7702748045049482511"))
# # print(ds.get_video_text("https://www.douyin.com/video/7699763112949337380"))
# print(ds.get_video_text("https://www.douyin.com/video/7694533724885745393"))
# exit()


if __name__ == '__main__':
    # 解决终端中文乱码问题
    sys.stdout.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(description='抖音爬虫工具：下载视频 / 获取文案 / 获取描述')
    sub = parser.add_subparsers(dest='command', required=True)

    p_text = sub.add_parser('text', help='获取视频文案（语音转文字，需阿里云 API Key）')
    p_text.add_argument('link', help='抖音视频公网链接')

    p_desc = sub.add_parser('desc', help='获取视频描述/标题')
    p_desc.add_argument('link', help='抖音视频公网链接')

    p_dl = sub.add_parser('download', help='下载视频到本地')
    p_dl.add_argument('link', help='抖音视频公网链接')
    p_dl.add_argument('save_path', nargs='?', default='.', help='保存目录，默认当前目录')

    p_uv = sub.add_parser('videos', help='提取博主主页指定数量的视频链接')
    p_uv.add_argument('user_url', help='抖音博主主页链接')
    p_uv.add_argument('count', nargs='?', type=int, default=10, help='需要提取的视频数量，默认10')
    p_uv.add_argument('--show-browser', action='store_true', help='显示浏览器以便手动登录或验证')
    p_uv.add_argument('--browser-profile', help='独立浏览器配置目录，用于保存登录状态')
    p_uv.add_argument('--login-timeout', type=int, default=0, help='等待手动登录的秒数，需配合 --show-browser')

    args = parser.parse_args()
    dy = DouyinSpider(
        headless=not getattr(args, 'show_browser', False),
        browser_profile=getattr(args, 'browser_profile', None),
    )

    try:
        if args.command == 'text':
            print(dy.get_video_text(args.link))
        elif args.command == 'desc':
            print(dy.get_video_link(args.link)[1])
        elif args.command == 'download':
            dy.download_video(args.link, args.save_path)
        elif args.command == 'videos':
            for link in dy.get_user_videos(args.user_url, args.count, args.login_timeout):
                print(link)
    except (RuntimeError, ValueError, requests.RequestException, OSError) as exc:
        print(f'操作失败：{exc}', file=sys.stderr)
        sys.exit(1)



