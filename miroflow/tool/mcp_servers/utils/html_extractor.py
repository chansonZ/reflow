from urllib.parse import urljoin, urlparse
import re
import requests
from bs4 import BeautifulSoup
import chardet
import unicodedata

class ReadabilityExtractor:
    """
    精确版基于Readability算法的网页正文提取器
    修复了类名匹配问题，改进了干扰元素过滤
    """
    
    def __init__(self):
        # 设置请求头，模拟真实浏览器访问
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }
        
        # 常见的正文容器选择器
        self.content_selectors = [
            '.content', '.main-content', '.post-content', 
            '.article-content', '.entry-content', '.story-body',
            '#content', '#main', '#article', '#post',
            'article', 'main'
        ]
        
        # 需要保留的头部元素（包含重要信息的）
        self.preserved_header_selectors = [
            '[property*="title"]', '[property*="author"]', '[property*="date"]',
            '[name="description"]', '[name="keywords"]'
        ]
        
        # 要移除的干扰元素 - 使用更精确的正则表达式
        self.remove_patterns = [
            r'.*ad.*',           # 包含ad的类名
            r'.*(adv|advertise|advertisement).*',  # 广告相关
            r'.*(sponsor|sponsored).*',           # 赞助相关
            r'.*(promo|promotion).*',             # 推广相关
            r'.*(recommend|related|similar).*',   # 推荐相关
            r'.*(hot|popular|trend).*',          # 热门相关
            r'.*(like|love|share|social).*',     # 社交分享
            r'.*(tag|category|archive).*',       # 分类标签
            r'.*(comment|reply|discuss).*',      # 评论相关
            r'.*(nav|navigation|menu|breadcrumb).*', # 导航相关
            r'.*(footer|copyright|legal).*',     # 页脚相关
            r'.*(widget|sidebar|aside).*',       # 侧边栏小工具
            r'.*(search|form|input).*',          # 搜索表单
        ]
        
        # 要移除的精确类名
        self.remove_exact_classes = {
            'ad', 'ads', 'advertisement', 'ad-container', 
            'ad-wrapper', 'sponsored', 'promo', 'promotion',
            'recommend', 'recommended', 'related-posts',
            'popular-posts', 'similar-articles', 'more-stories',
            'social-share', 'share-buttons', 'like-box',
            'comment-form', 'comments-area', 'disqus_thread',
            'disqus-comment', 'fb-comments', 'comment-list',
            'tags', 'categories', 'archives', 'meta',
            'breadcrumbs', 'breadcrumb', 'pagination',
            'pager', 'nav', 'navigation', 'menu',
            'footer', 'copyright', 'legal', 'disclaimer',
            'widget', 'widget-area', 'sidebar', 'aside',
            'search-form', 'search-box', 'newsletter',
            'signup', 'subscribe', 'subscription',
            'popup', 'modal', 'overlay', 'lightbox',
            'sticky', 'fixed', 'float', 'floating',
            'taboola', 'outbrain', 'doubleclick',
            'google-ads', 'adsense', 'adwords',
        }
        
        # 评分权重配置
        self.score_weights = {
            'text_length': 1.0,
            'paragraph_count': 15,
            'heading_count': 12,
            'link_density_penalty': 0.5,
            'list_count': 8,
            'image_count': 5,
            'class_name_bonus': {'content', 'article', 'post', 'entry'},
            'id_name_bonus': {'content', 'main', 'article', 'post', 'entry'}
        }
    
    def extract_content(self, html_content, url=None):
        """
        提取网页正文内容
        
        Args:
            html_content (str): HTML字符串
            url (str): 原始URL，用于处理相对路径
        
        Returns:
            dict: 包含标题、正文、作者等信息的字典
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 提取标题
        title = self._extract_title(soup)
        
        # 清理HTML，保留有用信息
        self._improved_clean_html(soup)
        
        # 寻找正文容器
        content_element = self._find_content_element(soup)
        
        if content_element:
            # 提取正文文本，保留结构
            text = self._get_structured_text_content(content_element)
            
            # 提取作者信息
            author = self._extract_author(soup)
            
            # 提取发布日期
            date = self._extract_date(soup)
            
            # 提取关键图片
            images = self._extract_images(content_element, url)
            
            return {
                'title': title,
                'content': text,
                'author': author,
                'date': date,
                'images': images,
                'success': True
            }
        else:
            return {
                'title': title,
                'content': '',
                'author': '',
                'date': '',
                'images': [],
                'success': False,
                'error': '未能找到正文内容'
            }
    
    def _extract_title(self, soup):
        """提取页面标题"""
        # 优先级：meta[property='og:title'] > title > h1 > h2
        og_title = soup.find('meta', attrs={'property': 'og:title'})
        if og_title and og_title.get('content'):
            return og_title.get('content').strip()
        
        title_tag = soup.find('title')
        if title_tag:
            return title_tag.get_text().strip()
        
        h1_tag = soup.find('h1')
        if h1_tag:
            return h1_tag.get_text().strip()
        
        h2_tag = soup.find('h2')
        if h2_tag:
            return h2_tag.get_text().strip()
        
        return ''
    
    def _extract_author(self, soup):
        """提取作者信息"""
        # 尝试多种方式获取作者
        selectors = [
            '[rel="author"]', 
            '.author', 
            '.byline', 
            '[property*="author"]',
            '[name="author"]',
            '.post-author',
            '.article-author',
            '[property="article:author"]'
        ]
        
        for selector in selectors:
            element = soup.select_one(selector)
            if element:
                author_text = element.get_text().strip()
                if author_text:
                    # 清理常见的前缀
                    author_text = re.sub(r'^作者[:：]\s*', '', author_text)
                    author_text = re.sub(r'^by\s+', '', author_text, flags=re.I)
                    return author_text
        
        # 尝试meta标签
        meta_author = soup.find('meta', attrs={'name': 'author'})
        if meta_author:
            return meta_author.get('content', '').strip()
        
        return ''
    
    def _extract_date(self, soup):
        """提取发布日期"""
        selectors = [
            '[datetime]',
            '[pubdate]',
            '.publish-date',
            '.date',
            '.time',
            '[property*="date"]',
            '[property="article:published_time"]'
        ]
        
        for selector in selectors:
            element = soup.select_one(selector)
            if element:
                date_text = element.get_text().strip()
                if self._is_valid_date(date_text):
                    return date_text
        
        # 尝试meta标签
        meta_date = soup.find('meta', attrs={'property': re.compile(r'date', re.I)})
        if meta_date and meta_date.get('content'):
            date_content = meta_date.get('content').strip()
            if self._is_valid_date(date_content):
                return date_content
        
        return ''
    
    def _is_valid_date(self, text):
        """判断文本是否为有效日期格式"""
        if not text:
            return False
            
        date_patterns = [
            r'\d{4}-\d{2}-\d{2}',  # YYYY-MM-DD
            r'\d{4}/\d{2}/\d{2}',  # YYYY/MM/DD
            r'\d{2}/\d{2}/\d{4}',  # MM/DD/YYYY
            r'\d{4}年\d{1,2}月\d{1,2}日',  # 中文日期
            r'\d{4}\.\d{2}\.\d{2}',  # YYYY.MM.DD
        ]
        return any(re.search(pattern, text) for pattern in date_patterns)
    
    def _extract_images(self, content_element, base_url=None):
        """提取正文中的图片"""
        images = []
        img_tags = content_element.find_all('img')
        
        for img in img_tags:
            src = img.get('src') or img.get('data-src') or img.get('data-original')
            if src:
                if base_url:
                    src = urljoin(base_url, src)
                
                alt = img.get('alt', '')
                title = img.get('title', '')
                
                # 获取图片尺寸信息（如果有）
                width = img.get('width')
                height = img.get('height')
                
                images.append({
                    'src': src,
                    'alt': alt,
                    'title': title,
                    'width': width,
                    'height': height
                })
        
        return images[:5]  # 最多返回5张图片
    
    def _should_remove_element(self, element):
        """判断元素是否应该被移除"""
        # 获取类名和ID
        class_names = element.get('class', [])
        if isinstance(class_names, str):
            class_names = [class_names]
        class_str = ' '.join(class_names).lower()
        
        id_str = element.get('id', '').lower()
        
        # 检查精确匹配
        for cls in class_names:
            if cls.lower() in self.remove_exact_classes:
                return True
        
        # 检查模式匹配
        combined_str = f"{class_str} {id_str}"
        for pattern in self.remove_patterns:
            if re.search(pattern, combined_str, re.IGNORECASE):
                return True
        
        # 检查标签名
        tag_name = element.name.lower()
        if tag_name in ['iframe', 'object', 'embed', 'canvas', 'applet', 'frame', 'frameset']:
            return True
            
        return False
    
    def _improved_clean_html(self, soup):
        """改进的HTML清理，保留有用信息"""
        # 移除脚本和样式
        for tag in soup(['script', 'style', 'noscript', 'svg']):
            tag.decompose()
        
        # 保留头部的重要meta信息
        preserved_elements = set()
        for selector in self.preserved_header_selectors:
            for tag in soup.select(selector):
                preserved_elements.add(id(tag))
        
        # 查找所有需要移除的元素
        elements_to_remove = []
        for element in soup.find_all():
            if id(element) not in preserved_elements and self._should_remove_element(element):
                elements_to_remove.append(element)
        
        # 移除元素
        for element in elements_to_remove:
            element.decompose()
        
        # 移除过于复杂的嵌套结构
        self._remove_excessive_nesting(soup)
        
        # 移除空标签（但保留有特殊意义的空标签如br、img、meta等）
        for tag in soup.find_all(lambda x: not x.get_text(strip=True) and 
                                x.name not in ['br', 'hr', 'img', 'input', 'meta', 'link', 'source']):
            if tag.name not in ['p', 'div', 'span', 'li', 'td', 'th']:
                tag.decompose()
    
    def _remove_excessive_nesting(self, soup):
        """移除过度嵌套的标签结构"""
        # 这里可以进一步实现简化逻辑，目前先跳过
        pass
    
    def _find_content_element(self, soup):
        """寻找正文容器元素"""
        # 方法1: 尝试根据常见类名查找
        for selector in self.content_selectors:
            element = soup.select_one(selector)
            if element and self._is_likely_content(element):
                return element
        
        # 方法2: 计算各元素的综合得分
        candidates = soup.find_all(['div', 'article', 'section', 'main', 'article'])
        best_candidate = None
        highest_score = 0
        
        for candidate in candidates:
            score = self._calculate_content_score(candidate)
            if score > highest_score:
                highest_score = score
                best_candidate = candidate
        
        # 如果还是找不到，使用body
        if not best_candidate:
            best_candidate = soup.find('body') or soup
        
        return best_candidate
    
    def _is_likely_content(self, element):
        """判断元素是否可能是正文内容"""
        text = element.get_text()
        text_length = len(text.strip())
        
        # 文本长度至少要有50个字符
        if text_length < 50:
            return False
        
        # 计算链接密度
        all_text = element.get_text()
        link_texts = [a.get_text() for a in element.find_all('a')]
        total_link_text = ''.join(link_texts)
        
        if len(all_text) > 0:
            link_density = len(total_link_text) / len(all_text)
            # 链接密度不应过高
            if link_density > 0.8:
                return False
        
        return True
    
    def _calculate_content_score(self, element):
        """计算元素的改进内容得分"""
        text = element.get_text()
        text_length = len(text.strip())
        
        if text_length == 0:
            return 0
        
        # 检查是否为明确的广告/推荐内容
        class_names = element.get('class', [])
        if isinstance(class_names, str):
            class_names = [class_names]
        class_str = ' '.join(class_names).lower()
        id_str = element.get('id', '').lower()
        combined_str = f"{class_str} {id_str}".lower()
        
        # 如果包含广告关键词，给予极低分数
        ad_keywords = ['ad', 'advert', 'sponsor', 'recommend', 'related', 'promo']
        ad_penalty = sum(100 for keyword in ad_keywords if keyword in combined_str)
        if ad_penalty > 0:
            return -ad_penalty * 100  # 强烈惩罚广告内容
        
        # 基础分数是文本长度
        score = text_length * self.score_weights['text_length']
        
        # 链接密度惩罚
        all_text = element.get_text()
        link_texts = [a.get_text() for a in element.find_all('a')]
        total_link_text = sum(len(t) for t in link_texts)
        
        if len(all_text) > 0:
            link_density = total_link_text / len(all_text)
            score -= link_density * text_length * self.score_weights['link_density_penalty']
        
        # 段落数加分
        paragraphs = element.find_all('p')
        paragraph_count = len([p for p in paragraphs if p.get_text().strip()])
        score += paragraph_count * self.score_weights['paragraph_count']
        
        # 标题数加分
        headings = element.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
        score += len(headings) * self.score_weights['heading_count']
        
        # 列表加分
        lists = element.find_all(['ul', 'ol'])
        list_items = element.find_all('li')
        score += len(lists) * self.score_weights['list_count'] // 2
        score += len(list_items) * self.score_weights['list_count'] // 4
        
        # 图片加分
        images = element.find_all('img')
        score += len(images) * self.score_weights['image_count']
        
        # 类名和ID加分
        combined_attrs = f"{class_str} {id_str}".lower()
        for bonus_class in self.score_weights['class_name_bonus']:
            if bonus_class in combined_attrs:
                score += 50
        
        for bonus_id in self.score_weights['id_name_bonus']:
            if bonus_id in combined_attrs:
                score += 50
        
        # 惩罚：如果元素太小
        if text_length < 100:
            score *= 0.1
        elif text_length < 300:
            score *= 0.5
        
        return max(score, 0)
    
    def _get_structured_text_content(self, element):
        """从元素中提取保持结构的文本内容"""
        content_parts = []
        
        def process_element(elem):
            if elem.name == 'p':
                # 段落标签，添加前后换行
                text = elem.get_text().strip()
                if text:
                    content_parts.append(text)
                    content_parts.append('\n\n')
            elif elem.name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
                # 标题标签，加粗显示
                text = elem.get_text().strip()
                if text:
                    content_parts.append(f"\n\n【{text}】\n\n")
            elif elem.name in ['li']:
                # 列表项，添加项目符号
                text = elem.get_text().strip()
                if text:
                    content_parts.append(f"• {text}\n")
            elif elem.name in ['br']:
                # 换行标签
                content_parts.append('\n')
            elif elem.name in ['div', 'section', 'article']:
                # 容器标签，递归处理子元素
                for child in elem.children:
                    if hasattr(child, 'name'):
                        process_element(child)
            elif elem.name is None:
                # 文本节点
                text = str(elem).strip()
                if text:
                    # 清理多余的空白字符
                    text = re.sub(r'\s+', ' ', text)
                    content_parts.append(text)
            else:
                # 其他标签，获取文本内容
                text = elem.get_text().strip()
                if text:
                    content_parts.append(text)
        
        process_element(element)
        
        # 合并内容并清理多余的换行
        content = ''.join(content_parts)
        
        # 清理多余的连续换行
        content = re.sub(r'\n\s*\n\s*\n+', '\n\n', content)
        
        # 清理行首行尾空白
        content = '\n'.join(line.strip() for line in content.split('\n'))
        
        return content.strip()


def detect_encoding(content_bytes):
    """检测字节流的编码"""
    detected = chardet.detect(content_bytes)
    encoding = detected['encoding']
    
    # 如果检测到的是ASCII或UTF-8，但置信度不高，尝试UTF-8
    if detected['confidence'] < 0.9 and encoding in ['ascii', 'utf-8']:
        try:
            content_bytes.decode('utf-8')
            return 'utf-8'
        except UnicodeDecodeError:
            pass
    
    return encoding


def extract_webpage_content(url_or_html, is_url=True):
    """
    提取网页正文内容的主函数
    
    Args:
        url_or_html (str): URL地址或HTML内容
        is_url (bool): 是否为URL地址，False表示直接传入HTML内容
    
    Returns:
        dict: 提取的结果
    """
    extractor = ReadabilityExtractor()
    
    if is_url:
        try:
            # 使用session复用连接
            session = requests.Session()
            session.headers.update(extractor.headers)
            
            # 发送请求
            response = session.get(
                url_or_html,
                timeout=(10, 30),  # (连接超时, 读取超时)
                allow_redirects=True
            )
            response.raise_for_status()
            
            # 处理编码
            content_bytes = response.content
            encoding = detect_encoding(content_bytes)
            
            # 尝试从HTTP头获取编码
            http_encoding = response.encoding
            if http_encoding:
                try:
                    html_content = content_bytes.decode(http_encoding)
                except (UnicodeDecodeError, LookupError):
                    # 如果HTTP头指定的编码失败，使用检测到的编码
                    html_content = content_bytes.decode(encoding)
            else:
                # 使用检测到的编码
                html_content = content_bytes.decode(encoding)
            
            return extractor.extract_content(html_content, url_or_html)
            
        except requests.exceptions.RequestException as e:
            return {
                'title': '',
                'content': '',
                'author': '',
                'date': '',
                'images': [],
                'success': False,
                'error': f'网络请求失败: {str(e)}'
            }
        except UnicodeDecodeError as e:
            return {
                'title': '',
                'content': '',
                'author': '',
                'date': '',
                'images': [],
                'success': False,
                'error': f'编码解码失败: {str(e)}'
            }
        except Exception as e:
            return {
                'title': '',
                'content': '',
                'author': '',
                'date': '',
                'images': [],
                'success': False,
                'error': f'未知错误: {str(e)}'
            }
    else:
        return extractor.extract_content(url_or_html)
    

def truncate_content_for_model(content,
                               info_to_extract,
                               model_max_len=32000,
                               EXTRACT_INFO_PROMPT="",
                               safety_factor=1.5):
    """
    根据模型最大长度截断内容
    
    参数:
    - content: 原始文本内容
    - info_to_extract: 需要提取的信息
    - model_max_len: 模型最大输入长度 (默认32000)
    - EXTRACT_INFO_PROMPT: 提取信息的提示词
    - safety_factor: 安全系数，默认1.5
    
    返回:
    - 截断后的内容
    """

    # 计算当前的总token数（这里用字符数近似模拟）
    t = len(EXTRACT_INFO_PROMPT) + len(info_to_extract) + len(content)

    # 计算目标长度
    target_length = int(model_max_len * safety_factor)

    if t <= target_length:
        # 如果已经满足条件，直接返回原内容
        return content

    # 计算需要减少的字符数
    chars_to_reduce = t - target_length

    # 从content中截掉多余的部分
    # 保留开头的部分，因为通常重要信息在前面
    new_content_length = len(content) - chars_to_reduce

    # 确保新长度至少为0
    new_content_length = max(0, new_content_length)

    # 截断content
    truncated_content = content[:new_content_length]

    # print(f"内容被截断: 从 {len(content)} 字符缩减到 {len(truncated_content)} 字符")
    # print(
    #     f"原始总长度: {t} -> 目标: {target_length}，当前总长：{ len(EXTRACT_INFO_PROMPT) + len(info_to_extract) + len(truncated_content)}"
    # )

    return truncated_content    