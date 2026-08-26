"""Job scraping module - Extract jobs from BOSS直聘 search results."""

import json
import random
import re
import time
import hashlib
from urllib.parse import quote

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from bosshunter.browser import (
    new_tab, close_tab, evaluate, scroll, wait_for_load
)
from bosshunter.cancellation import stop_requested
from bosshunter.config import CITY_CODES
from bosshunter.db import get_db, job_exists, insert_job, update_job_company_info
from bosshunter.job_filters import matching_deal_breaker
from bosshunter.throttle import PageThrottle


def _normalize_company_size(value: str) -> str:
    """Normalize scraped company size strings to BOSS filter categories."""
    value = (value or "").strip().replace(" ", "")

    # Exact standard labels first (handles the normal BOSS display text)
    for label in ["10000人以上", "1000-9999人", "500-999人", "100-499人", "20-99人", "0-20人"]:
        if value == label or value == f"{label}及以上":
            return label

    # Variants with 万 -> 10000人以上
    if "万" in value and "人" in value:
        nums = re.findall(r"\d+", value)
        if nums and int(nums[0]) >= 1:
            return "10000人以上"

    # Extract numbers and classify by the largest value
    nums = [int(n) for n in re.findall(r"\d+", value)]
    if not nums:
        return value

    max_num = max(nums)
    min_num = min(nums)

    if max_num >= 10000:
        return "10000人以上"
    if max_num >= 1000:
        return "1000-9999人"
    if max_num >= 500:
        return "500-999人"
    if max_num >= 100:
        return "100-499人"
    if max_num >= 20:
        # 0-20 vs 20-99 overlap; prefer 0-20 when the range clearly starts at 0
        if min_num < 20:
            return "0-20人"
        return "20-99人"
    return "0-20人"


def _match_company_size(company_size: str | None, allowed: list[str]) -> bool:
    """Return True if company_size matches one of the configured size filters."""
    if not allowed:
        return True
    company_size = (company_size or "").strip()
    if not company_size:
        return False
    normalized = _normalize_company_size(company_size)
    return normalized in allowed or company_size in allowed


console = Console()

# BOSS直聘搜索页 URL 模板
SEARCH_URL = "https://www.zhipin.com/web/geek/job?query={keyword}&city={city_code}"

# BOSS 直聘「公司规模」筛选的 URL 参数编码（与 ?scale=301,302,... 对应）。
# 这些值是 BOSS 内部固定的，按列表顺序 0-20 → 10000人以上 递增。
COMPANY_SIZE_SCALE_CODES = {
    "0-20人": 301,
    "20-99人": 302,
    "100-499人": 303,
    "500-999人": 304,
    "1000-9999人": 305,
    "10000人以上": 306,
}

# JS: 模拟点击页面顶部「公司规模」筛选器，勾选指定规模后再抓取。
# 注意：BOSS 直聘的"公司规模"筛选每次勾选一个选项都会立即改 URL (?scale=...) 并刷新
# 列表（不是一次性多选弹层），所以走 DOM 点击在多选时极不稳定。
# 现在的做法是直接在 Python 端把 ?scale=... 拼到搜索 URL 上，由 BOSS 服务端一次性返回
# 筛选结果；这里保留脚本只是为了在 URL 拼不上的情况下做兜底 UI 操作。
# 入参通过 window.__sizeFilterTargets 传入（字符串数组，如 ["0-20人","20-99人"]）。
# 返回 "applied:N" / "no-targets" / "no-trigger" / "no-panel" / "not-found:..."。
JS_APPLY_SIZE_FILTER = r"""
(() => {
    const targets = (window.__sizeFilterTargets || []).map(s => String(s).trim());
    if (!targets.length) return "no-targets";

    const norm = (s) => (s || '').replace(/\s+/g, '');

    // 1) 定位「公司规模」筛选框：沿文本节点向上找最近的 .condition-filter-select
    let box = null;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    let node;
    while ((node = walker.nextNode())) {
        if (norm(node.textContent).includes('公司规模')) {
            let el = node.parentElement;
            while (el && !(el.className && el.className.includes && el.className.includes('condition-filter-select'))) {
                el = el.parentElement;
            }
            if (el) { box = el; break; }
        }
    }
    if (!box) return "no-trigger";
    const trigger = box.querySelector('.current-select') || box;

    // 2) 查找弹层选项（每次调用都重新查询，避免 Vue 重渲染后旧引用失效）
    const findOptions = () => {
        const inBox = Array.from(box.querySelectorAll('li'));
        if (inBox.length) return inBox;
        const pop = box.querySelector('.option-list, .options, ul') || box.nextElementSibling;
        if (pop) return Array.from(pop.querySelectorAll('li'));
        return [];
    };

    // 打开弹层
    trigger.click();

    // 判断选项是否已选中（避免重复点击反向取消勾选）
    const isOptionSelected = (opt) => {
        const cls = opt.className || '';
        if (cls.includes('active') || cls.includes('selected') || cls.includes('checked') || cls.includes('cur')) return true;
        if (opt.querySelector('input[type=checkbox]:checked, input[type=radio]:checked')) return true;
        return false;
    };

    // 3) 逐个勾选目标规模：每次重新查找选项，点击后 DOM 可能重渲染
    let matched = 0;
    const missing = [];
    for (const target of targets) {
        let opts = findOptions();
        let opt = opts.find(o => norm(o.textContent).includes(norm(target)));

        // 面板可能在上一次点击后关闭，回退重开
        if (!opt) {
            trigger.click();
            opts = findOptions();
            opt = opts.find(o => norm(o.textContent).includes(norm(target)));
        }
        if (!opt) { missing.push(target); continue; }

        if (!isOptionSelected(opt)) {
            opt.click();
        }
        matched++;
    }
    if (!matched) return "not-found:" + missing.join(',');

    // 4) 点击确定（弹层内的确认按钮，或文本含「确定」「完成」的按钮）
    const confirm = box.querySelector('.confirm-btn, .btn-confirm, button.confirm')
        || Array.from(document.querySelectorAll('button')).find(b => { const t = norm(b.textContent); return t.includes('确定') || t.includes('完成'); });
    if (confirm) confirm.click();
    return "applied:" + matched + "/" + targets.length;
})()
"""

# JS: 从搜索列表页提取岗位卡片数据
JS_EXTRACT_LIST = r"""
(() => {
    const wraps = document.querySelectorAll('.job-card-wrap');
    const jobs = [];
    const sizeRegex = /(\d+-\d+人|\d+人以上|\d+人及以上|少于\d+人|\d+~\d+人|\d+到\d+人|\d+人)/;
    wraps.forEach((wrap) => {
        const box = wrap.querySelector('.job-card-box') || wrap;
        const nameEl = box.querySelector('.job-name');
        const salaryEl = box.querySelector('.job-salary');
        const tags = box.querySelectorAll('.tag-list li');
        const companyEl = box.querySelector('.boss-name') || box.querySelector('.company-name');
        const locationEl = box.querySelector('.company-location');
        const href = nameEl ? nameEl.getAttribute('href') : '';

        if (!nameEl || !href) return;

        // Best-effort: pull company size / industry from the card sub-info so we
        // can pre-filter without opening the detail page. BOSS cards vary in DOM,
        // so we scan a broad set of likely containers and then fall back to all
        // small text fragments in the card.
        let company_size = '';
        let company_industry = '';

        // 1st pass: known sub-info selectors
        const infoSelectors = '.company-location, .company-info, .company-text, .company-tag, .company-tags, .company-desc, .boss-info__description, .job-card-company';
        const infoEls = box.querySelectorAll(infoSelectors);
        for (const el of infoEls) {
            const txt = (el.textContent || '').trim();
            if (!company_size) {
                const m = txt.match(sizeRegex);
                if (m) company_size = m[0];
            }
            if (!company_industry && txt.length <= 32 && !txt.includes('人')) {
                company_industry = txt;
            }
        }

        // 2nd pass: if still no size, scan every small text node in the card
        // (excluding title/salary/tag/location/company-name) to catch variant layouts.
        if (!company_size) {
            const excludeSelectors = '.job-name, .job-salary, .tag-list, .company-name, .boss-name, .company-location, button, a';
            const excludeEls = new Set();
            box.querySelectorAll(excludeSelectors).forEach(el => excludeEls.add(el));
            const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT, null, false);
            let node;
            while ((node = walker.nextNode())) {
                if (Array.from(excludeEls).some(ex => ex.contains(node))) continue;
                const txt = (node.textContent || '').trim();
                if (!txt || txt.length > 64) continue;
                const m = txt.match(sizeRegex);
                if (m) {
                    company_size = m[0];
                    break;
                }
            }
        }

        jobs.push({
            title: nameEl.textContent.trim(),
            salary: salaryEl ? salaryEl.textContent.trim() : '',
            experience: tags[0] ? tags[0].textContent.trim() : '',
            education: tags[1] ? tags[1].textContent.trim() : '',
            company: companyEl ? companyEl.textContent.trim() : '',
            location: locationEl ? locationEl.textContent.trim() : '',
            company_size: company_size,
            company_industry: company_industry,
            url: href
        });
    });
    return JSON.stringify(jobs);
})()
"""

# JS: 从详情页提取完整岗位信息
JS_EXTRACT_DETAIL = r"""
(() => {
    const info = {};
    // Title and salary
    info.title = document.querySelector('.info-primary .name h1')?.textContent?.trim()
        || document.querySelector('.name h1')?.textContent?.trim()
        || document.title.split('-')[0]?.trim();
    info.salary = document.querySelector('.info-primary .salary')?.textContent?.trim()
        || document.querySelector('.salary')?.textContent?.trim() || '';

    // Tags (experience, education, etc)
    const tagItems = document.querySelectorAll('.info-primary .tag-list span');
    const tagTexts = Array.from(tagItems).map(t => t.textContent.trim());
    info.experience = tagTexts[0] || '';
    info.education = tagTexts[1] || '';

    // JD
    info.jd = document.querySelector('.job-sec-text')?.textContent?.trim() || '';

    // Company info - try multiple selectors
    const companyLinks = document.querySelectorAll('.sider-company .company-info a');
    info.company = '';
    for (const link of companyLinks) {
        const text = link.textContent.trim();
        if (text && text.length > 0 && !text.includes('http')) {
            info.company = text;
            break;
        }
    }
    if (!info.company) {
        // Fallback: extract from page title "「职位」_公司名招聘"
        const titleMatch = document.title.match(/_(.+?)招聘/);
        info.company = titleMatch ? titleMatch[1] : '';
    }

    // Company details
    info.company_size = '';
    info.company_industry = '';

    // Strategy 1: BOSS's own structured tags
    const companyItems = Array.from(document.querySelectorAll('.sider-company .res-industry-item, .company-info-item'))
        .map(el => (el.textContent || '').trim())
        .filter(t => t && t.length <= 32);
    for (const t of companyItems) {
        if (!info.company_size && /人/.test(t)) info.company_size = t;
        else if (!info.company_industry && !/人/.test(t) && t !== info.company) info.company_industry = t;
    }

    // Strategy 2: fallback - scan the company sidebar text for size patterns
    if (!info.company_size) {
        const companySection = document.querySelector('.sider-company') || document.querySelector('.company-info');
        if (companySection) {
            const sizeRegex = /(\d+-\d+人|\d+人以上|\d+人及以上|少于\d+人|\d+~\d+人|\d+到\d+人|\d+人)/;
            const candidates = Array.from(companySection.querySelectorAll('p, span, a, li, div, h3, h4'))
                .map(el => (el.textContent || '').trim())
                .filter(t => t && t.length <= 32);
            for (const t of candidates) {
                const m = t.match(sizeRegex);
                if (m) {
                    info.company_size = m[0];
                    break;
                }
            }
        }
    }

    // HR info
    const bossSection = document.querySelector('.boss-info-attr') || document.querySelector('.job-boss-info');
    if (bossSection) {
        const nameEl = bossSection.querySelector('.name');
        const titleEl = bossSection.querySelector('.title');
        info.hr_name = nameEl?.textContent?.trim() || '';
        info.hr_title = titleEl?.textContent?.trim() || '';
    } else {
        info.hr_name = '';
        info.hr_title = '';
    }
    info.hr_active = document.querySelector('.boss-active-time')?.textContent?.trim() || '';

    // URL
    info.url = window.location.pathname;

    return JSON.stringify(info);
})()
"""


def _generate_job_id(url: str) -> str:
    """Generate a unique job ID from URL path."""
    # Extract the unique part from /job_detail/xxx.html
    match = re.search(r'/job_detail/([^.]+)', url)
    if match:
        return match.group(1)
    return hashlib.md5(url.encode()).hexdigest()[:16]


def _notify(config: dict, message: str) -> None:
    """Forward log messages to the workbench log callback if available."""
    callback = config.get("_workbench_log")
    if callable(callback):
        callback(message)


def _backfill_company_info(db, job_id: str, job_url: str, throttle: PageThrottle) -> None:
    """Re-open a detail page for an already-stored job to fill missing company info."""
    VALID_SIZES = {"10000人以上", "1000-9999人", "500-999人", "100-499人", "20-99人", "0-20人"}
    row = db.execute("SELECT company_size FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return
    existing = (row[0] or "").strip()
    if existing in VALID_SIZES:
        return
    throttle.wait()
    detail_url = f"https://www.zhipin.com{job_url}"
    detail_target = new_tab(detail_url, background=True)
    if not detail_target:
        return
    try:
        time.sleep(2)
        wait_for_load(detail_target, timeout=10)
        detail_result = evaluate(detail_target, JS_EXTRACT_DETAIL)
        if detail_result:
            try:
                detail = json.loads(detail_result)
                update_job_company_info(
                    db, job_id,
                    detail.get("company_size", ""),
                    detail.get("company_industry", ""),
                )
            except (json.JSONDecodeError, TypeError):
                pass
    finally:
        close_tab(detail_target)


def scrape_jobs(config: dict, keywords: list[str], limit: int | None = None) -> int:
    """Scrape jobs from BOSS直聘 and store in database.

    Supports multi-keyword × multi-city combinations with pagination.
    When limit is None, collection is bounded only by city × keyword × max_pages.
    Returns the number of new jobs added.
    """
    db = get_db()
    throttle = PageThrottle(delay_min=2.0, delay_max=5.0)
    deal_breakers = config.get("profile", {}).get("deal_breakers", [])
    company_size_filters = config.get("search", {}).get("company_sizes", [])
    new_count = 0

    # Pagination config
    search_config = config.get("search", {})
    max_pages = min(search_config.get("max_pages", 3), 10)  # Hard cap: 10 pages

    # Resolve cities: search.cities > profile.target_cities > ["北京"]
    cities = search_config.get("cities", [])
    if not cities:
        cities = config.get("profile", {}).get("target_cities", ["北京"])

    # Build search combinations: city × keyword
    search_combos = []
    for city in cities:
        city_code = CITY_CODES.get(city)
        if not city_code:
            console.print(f"[yellow]⚠ 未识别的城市: {city}，已跳过[/yellow]")
            continue
        for keyword in keywords:
            search_combos.append((city, city_code, keyword))

    if not search_combos:
        console.print("[red]没有有效的搜索组合（检查城市配置）[/red]")
        db.close()
        return 0

    console.print(f"[dim]搜索组合: {len(search_combos)} 个 ({len(cities)}城市 × {len(keywords)}关键词 × {max_pages}页)[/dim]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        for city, city_code, keyword in search_combos:
            if limit is not None and new_count >= limit:
                break
            if stop_requested(config):
                _notify(config, "用户已请求停止，正在结束采集...")
                break

            label = f"{city}/{keyword}" if len(cities) > 1 else keyword
            task = progress.add_task(f"搜索: {label}", total=None)
            keyword_new = 0

            for page in range(1, max_pages + 1):
                if limit is not None and new_count >= limit:
                    break
                if stop_requested(config):
                    _notify(config, "用户已请求停止，正在结束采集...")
                    break

                # Build paginated URL
                search_url = SEARCH_URL.format(keyword=quote(keyword), city_code=city_code)
                sort_mode = search_config.get("sort", "")
                if sort_mode == "newest":
                    search_url += "&sortType=2"
                # 公司规模多选：直接在 URL 上拼 ?scale=301,302,... 由 BOSS 服务端筛选。
                # 走 DOM 点击会因为 BOSS 每次勾选都刷新列表而不可靠（多选变反向取消）。
                # 注意：scale 必须对每一页都生效，否则第 2 页起会漏掉规模筛选，
                # 导致返回超出配置范围的岗位。
                if company_size_filters:
                    scale_codes = [str(COMPANY_SIZE_SCALE_CODES[s]) for s in company_size_filters if s in COMPANY_SIZE_SCALE_CODES]
                    if scale_codes:
                        search_url += "&scale=" + ",".join(scale_codes)
                if page > 1:
                    search_url += f"&page={page}"

                # Open search page
                target_id = new_tab(search_url, background=True)
                if not target_id:
                    if page == 1:
                        progress.update(task, description=f"[red]✗ 无法打开搜索页: {label}[/red]")
                    break

                time.sleep(3)
                wait_for_load(target_id, timeout=10)

                # 兜底：如果配置的规模值没有可识别的 scale 编码，回退到点击页面筛选器。
                # 正常情况下不会走到这里（所有规模标签都在 COMPANY_SIZE_SCALE_CODES 里）。
                if company_size_filters and page == 1 and not any(
                    s in COMPANY_SIZE_SCALE_CODES for s in company_size_filters
                ):
                    try:
                        evaluate(target_id, f"window.__sizeFilterTargets = {json.dumps(company_size_filters, ensure_ascii=False)};")
                        time.sleep(1.0)  # 等搜索页 Vue 完全渲染后再操作筛选器
                        apply_result = evaluate(target_id, JS_APPLY_SIZE_FILTER)
                        if apply_result and apply_result.startswith("applied"):
                            time.sleep(2)
                            progress.update(task, description=f"搜索: {label} (已应用规模筛选 {company_size_filters} -> {apply_result})")
                        else:
                            progress.update(task, description=f"搜索: {label} (规模筛选UI未匹配: {apply_result}，回退到列表页过滤)")
                    except Exception:
                        pass

                # Scroll to load all results on this page
                scroll(target_id, y=2000)
                time.sleep(1.5)
                scroll(target_id, y=4000)
                time.sleep(1.5)

                # Extract job list
                result = evaluate(target_id, JS_EXTRACT_LIST)
                if not result:
                    close_tab(target_id)
                    break

                try:
                    jobs_list = json.loads(result)
                except (json.JSONDecodeError, TypeError):
                    close_tab(target_id)
                    break

                close_tab(target_id)

                # No results on this page, stop pagination
                if not jobs_list:
                    break

                progress.update(task, description=f"搜索: {label} 第{page}页 ({len(jobs_list)}条)")

                # Process each job
                for job_data in jobs_list:
                    if limit is not None and new_count >= limit:
                        break
                    if stop_requested(config):
                        _notify(config, "用户已请求停止，正在结束采集...")
                        break

                    job_url = job_data.get("url", "")
                    job_id = _generate_job_id(job_url)

                    # Skip if already exists, but backfill missing company info
                    if job_exists(db, job_id):
                        _backfill_company_info(db, job_id, job_url, throttle)
                        continue

                    # Skip deal breakers
                    if matching_deal_breaker(job_data.get("title", ""), deal_breakers):
                        continue

                    # Pre-filter by company size using list-page data (avoids a detail-page request)
                    list_size = job_data.get("company_size", "")
                    if company_size_filters and list_size and not _match_company_size(list_size, company_size_filters):
                        progress.update(task, description=f"搜索: {label} 第{page}页 (列表页过滤规模 {list_size})")
                        continue

                    # Open detail page for full JD
                    throttle.wait()
                    detail_url = f"https://www.zhipin.com{job_url}"
                    detail_target = new_tab(detail_url, background=True)
                    if not detail_target:
                        continue

                    time.sleep(2)
                    wait_for_load(detail_target, timeout=10)

                    # Extract detail
                    detail_result = evaluate(detail_target, JS_EXTRACT_DETAIL)
                    close_tab(detail_target)

                    if not detail_result:
                        continue

                    try:
                        detail = json.loads(detail_result)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    # Build job record
                    job_record = {
                        "id": job_id,
                        "title": detail.get("title", job_data.get("title", "")),
                        "company": detail.get("company", job_data.get("company", "")),
                        "salary": detail.get("salary", job_data.get("salary", "")),
                        "city": city,
                        "experience": detail.get("experience", job_data.get("experience", "")),
                        "jd": detail.get("jd", ""),
                        "hr_name": detail.get("hr_name", ""),
                        "hr_title": detail.get("hr_title", ""),
                        "hr_active": detail.get("hr_active", ""),
                        "company_size": detail.get("company_size", "") or list_size,
                        "company_industry": detail.get("company_industry", ""),
                        "url": detail_url,
                    }

                    # Fallback filter: if list page lacked size, rely on detail page
                    if not _match_company_size(job_record.get("company_size", ""), company_size_filters):
                        continue

                    insert_job(db, job_record)
                    new_count += 1
                    keyword_new += 1
                    progress.update(task, description=f"搜索: {label} 第{page}页 (新增 {keyword_new})")

                # Anti-scraping: pause between pages
                if page < max_pages:
                    time.sleep(random.uniform(3.0, 6.0))

            progress.update(task, description=f"搜索: {label} (新增 {keyword_new})")

    if stop_requested(config):
        _notify(config, "采集已停止")
    db.close()
    return new_count
