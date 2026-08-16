"""
Blitz API client — thin wrapper around all Blitz API v2 endpoints.
Reference: shared/scripts/blitz_api_guide.md

Usage:
    from blitz_client import BlitzClient
    client = BlitzClient()  # reads BLITZ_API_KEY from shared/config/.env
    info = client.key_info()
    companies = client.company_search(industry=["IT Services"], country_code=["GB"])
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error

BASE_URL = "https://api.blitz-api.ai"

# Load BLITZ_API_KEY from SOS central .env if not already in environment
_ENV_FILE = os.path.join(os.path.dirname(__file__), "..", "config", ".env")
if os.path.exists(_ENV_FILE) and not os.environ.get("BLITZ_API_KEY"):
    with open(_ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line.startswith("BLITZ_API_KEY=") and not line.startswith("#"):
                os.environ["BLITZ_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                break


class BlitzError(Exception):
    def __init__(self, status, message, body=None):
        self.status = status
        self.message = message
        self.body = body
        super().__init__(f"Blitz API {status}: {message}")


class BlitzClient:
    # Total attempts per request (so 3 = original + 2 retries), backing off 1s then 2s.
    RETRIES = 3

    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ.get("BLITZ_API_KEY", "")
        if not self.api_key:
            raise BlitzError(0, "BLITZ_API_KEY not set. Add it to shared/config/.env or set as environment variable.")
        self._last_request_time = 0

    def _throttle(self):
        """Enforce 5 req/s rate limit (200ms between requests)."""
        elapsed = time.time() - self._last_request_time
        if elapsed < 0.21:
            time.sleep(0.21 - elapsed)
        self._last_request_time = time.time()

    def _request(self, method, path, body=None):
        """Send one request, retrying only what is genuinely transient.

        WHY THIS RETRIES
        A single dropped connection used to kill an entire multi-hour stage. Measured:
        a 1,046-company research pull died mid-run on WinError 10054 ("connection
        forcibly closed by the remote host") with the Blitz API perfectly healthy, and
        the whole stage was lost. `db.py` in the hiring pipeline already learned this
        lesson for Postgres; the Blitz side had no equivalent, so the longest-running
        stages were the least protected.

        WHAT IT RETRIES, AND WHAT IT DELIBERATELY DOES NOT
        Connection/timeout errors, 429, and 5xx only — the failures where the identical
        request may well succeed a second later. A 4xx is OUR bug (bad key, bad filter,
        malformed body); retrying it just repeats the same mistake more slowly and hides
        it, so those raise immediately as before.

        Retries are safe here because every Blitz endpoint this client touches is a read
        (search/enrich/lookup) — replaying one costs a call, never a duplicate write.
        """
        url = f"{BASE_URL}{path}"
        data = json.dumps(body).encode("utf-8") if body else None
        last = None
        for attempt in range(self.RETRIES):
            # Throttle inside the loop: a retry is a real request and must respect the
            # 5 req/s account limit too, or a burst of retries triggers the 429s it is
            # trying to recover from.
            self._throttle()
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("x-api-key", self.api_key)
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                body_text = ""
                try:
                    body_text = e.read().decode("utf-8")
                except Exception:
                    pass
                err = BlitzError(e.code, body_text or str(e), body_text)
                if e.code != 429 and e.code < 500:
                    raise err          # our bug — surface it now, do not mask it
                last = err
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                # URLError wraps the socket errors (incl. ConnectionResetError 10054);
                # a bare timeout surfaces as TimeoutError/OSError depending on platform.
                last = BlitzError(0, f"{type(e).__name__}: {e}")
            if attempt < self.RETRIES - 1:
                time.sleep(2 ** attempt)       # 1s, 2s, 4s
        raise last

    # --- Account ---

    def key_info(self):
        """GET /v2/account/key-info — check credits, plan, rate limits."""
        return self._request("GET", "/v2/account/key-info")

    # --- Search ---

    def company_search(self, *, name_include=None, name_exclude=None,
                       industry=None, industry_exclude=None,
                       company_type=None, company_type_exclude=None,
                       employee_range=None,
                       employee_min=None, employee_max=None,
                       keywords=None, keywords_exclude=None,
                       founded_year_min=None, founded_year_max=None,
                       city_include=None, city_exclude=None,
                       country_code=None, continent=None, sales_region=None,
                       min_linkedin_followers=None,
                       max_results=10, cursor=None):
        """POST /v2/search/companies — find companies matching filters."""
        company = {}
        if name_include or name_exclude:
            company["name"] = {}
            if name_include: company["name"]["include"] = name_include
            if name_exclude: company["name"]["exclude"] = name_exclude
        if industry or industry_exclude:
            company["industry"] = {}
            if industry: company["industry"]["include"] = industry
            if industry_exclude: company["industry"]["exclude"] = industry_exclude
        if company_type or company_type_exclude:
            company["type"] = {}
            if company_type: company["type"]["include"] = company_type
            if company_type_exclude: company["type"]["exclude"] = company_type_exclude
        if employee_range:
            company["employee_range"] = employee_range
        if employee_min is not None or employee_max is not None:
            company["employee_count"] = {}
            if employee_min is not None: company["employee_count"]["min"] = employee_min
            if employee_max is not None: company["employee_count"]["max"] = employee_max
        if keywords or keywords_exclude:
            company["keywords"] = {}
            if keywords: company["keywords"]["include"] = keywords
            if keywords_exclude: company["keywords"]["exclude"] = keywords_exclude
        if founded_year_min is not None or founded_year_max is not None:
            company["founded_year"] = {}
            if founded_year_min is not None: company["founded_year"]["min"] = founded_year_min
            if founded_year_max is not None: company["founded_year"]["max"] = founded_year_max
        hq = {}
        if city_include or city_exclude:
            hq["city"] = {}
            if city_include: hq["city"]["include"] = city_include
            if city_exclude: hq["city"]["exclude"] = city_exclude
        if country_code: hq["country_code"] = country_code
        if continent: hq["continent"] = continent
        if sales_region: hq["sales_region"] = sales_region
        if hq:
            company["hq"] = hq
        if min_linkedin_followers is not None:
            company["min_linkedin_followers"] = min_linkedin_followers

        body = {"max_results": max_results}
        if company:
            body["company"] = company
        if cursor is not None:
            body["cursor"] = cursor
        return self._request("POST", "/v2/search/companies", body)

    def people_search(self, *, company_linkedin_url=None,
                      company_name_include=None, company_industry=None,
                      company_type=None, company_employee_range=None,
                      company_country_code=None, company_continent=None,
                      company_keywords=None,
                      job_title_include=None, job_title_exclude=None,
                      include_linkedin_headline=False,
                      job_function=None, job_level=None,
                      min_connections=None,
                      person_country=None, person_continent=None,
                      person_city_include=None,
                      max_results=10, cursor=None):
        """POST /v2/search/people — find people matching company + person filters."""
        company = {}
        if company_linkedin_url:
            company["linkedin_url"] = company_linkedin_url if isinstance(company_linkedin_url, list) else [company_linkedin_url]
        if company_name_include:
            company["name"] = {"include": company_name_include}
        if company_industry:
            company["industry"] = {"include": company_industry}
        if company_type:
            company["type"] = {"include": company_type}
        if company_employee_range:
            company["employee_range"] = company_employee_range
        if company_country_code:
            company["hq"] = {"country_code": company_country_code}
        if company_continent:
            company.setdefault("hq", {})["continent"] = company_continent
        if company_keywords:
            company["keywords"] = {"include": company_keywords}

        people = {}
        if job_title_include or job_title_exclude:
            people["job_title"] = {}
            if job_title_include: people["job_title"]["include"] = job_title_include
            if job_title_exclude: people["job_title"]["exclude"] = job_title_exclude
        if include_linkedin_headline:
            people["include_linkedin_headline"] = True
        if job_function:
            people["job_function"] = job_function
        if job_level:
            people["job_level"] = job_level
        if min_connections is not None:
            people["min_connections"] = min_connections
        loc = {}
        if person_country: loc["country"] = person_country
        if person_continent: loc["continent"] = person_continent
        if person_city_include: loc["city"] = {"include": person_city_include}
        if loc:
            people["location"] = loc

        body = {"max_results": max_results}
        if company: body["company"] = company
        if people: body["people"] = people
        if cursor is not None: body["cursor"] = cursor
        return self._request("POST", "/v2/search/people", body)

    def employee_finder(self, company_linkedin_url, *,
                        country_code=None, continent=None, sales_region=None,
                        job_level=None, job_function=None,
                        min_connections_count=None,
                        max_results=10, page=1):
        """POST /v2/search/employee-finder — list employees at a specific company."""
        body = {"company_linkedin_url": company_linkedin_url,
                "max_results": max_results, "page": page}
        if country_code: body["country_code"] = country_code
        if continent: body["continent"] = continent
        if sales_region: body["sales_region"] = sales_region
        if job_level: body["job_level"] = job_level
        if job_function: body["job_function"] = job_function
        if min_connections_count is not None: body["min_connections_count"] = min_connections_count
        return self._request("POST", "/v2/search/employee-finder", body)

    def waterfall_icp_search(self, company_linkedin_url, cascade, *, max_results=5):
        """POST /v2/search/waterfall-icp-keyword — find best decision-maker via cascade tiers."""
        body = {
            "company_linkedin_url": company_linkedin_url,
            "cascade": cascade,
            "max_results": max_results
        }
        return self._request("POST", "/v2/search/waterfall-icp-keyword", body)

    # --- Enrichment ---

    def find_work_email(self, person_linkedin_url):
        """POST /v2/enrichment/email — get work email from LinkedIn profile URL."""
        return self._request("POST", "/v2/enrichment/email",
                             {"person_linkedin_url": person_linkedin_url})

    def find_phone(self, person_linkedin_url):
        """POST /v2/enrichment/phone — get mobile phone (E.164) from LinkedIn profile URL."""
        return self._request("POST", "/v2/enrichment/phone",
                             {"person_linkedin_url": person_linkedin_url})

    def company_enrichment(self, company_linkedin_url):
        """POST /v2/enrichment/company — get full company profile from LinkedIn URL."""
        return self._request("POST", "/v2/enrichment/company",
                             {"company_linkedin_url": company_linkedin_url})

    def domain_to_linkedin(self, domain):
        """POST /v2/enrichment/domain-to-linkedin — convert domain to LinkedIn company URL."""
        return self._request("POST", "/v2/enrichment/domain-to-linkedin",
                             {"domain": domain})

    # --- Pagination helpers ---

    def company_search_all(self, max_pages=10, **kwargs):
        """Iterate through all pages of company_search. Yields individual company dicts."""
        cursor = None
        for _ in range(max_pages):
            resp = self.company_search(cursor=cursor, **kwargs)
            results = resp.get("results", [])
            for r in results:
                yield r
            cursor = resp.get("cursor")
            if cursor is None:
                break

    def people_search_all(self, max_pages=10, **kwargs):
        """Iterate through all pages of people_search. Yields individual person dicts."""
        cursor = None
        for _ in range(max_pages):
            resp = self.people_search(cursor=cursor, **kwargs)
            results = resp.get("results", [])
            for r in results:
                yield r
            cursor = resp.get("cursor")
            if cursor is None:
                break

    def employee_finder_all(self, company_linkedin_url, max_pages=10, **kwargs):
        """Iterate through all pages of employee_finder. Yields individual employee dicts."""
        for page in range(1, max_pages + 1):
            resp = self.employee_finder(company_linkedin_url, page=page, **kwargs)
            results = resp.get("results", [])
            for r in results:
                yield r
            total_pages = resp.get("total_pages", 1)
            if page >= total_pages:
                break


# --- CLI quick-test ---

if __name__ == "__main__":
    client = BlitzClient()
    if len(sys.argv) < 2:
        print("Usage: python blitz_client.py <command> [args]")
        print("Commands: key-info, company-search, people-search, employee-finder,")
        print("          waterfall-icp, find-email, company-enrich, domain-to-linkedin")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "key-info":
        result = client.key_info()
        print(json.dumps(result, indent=2))

    elif cmd == "company-search":
        if len(sys.argv) > 2:
            body = json.loads(sys.argv[2])
            result = client._request("POST", "/v2/search/companies", body)
        else:
            result = client.company_search(country_code=["GB"], max_results=3)
        print(json.dumps(result, indent=2))

    elif cmd == "people-search":
        if len(sys.argv) > 2:
            body = json.loads(sys.argv[2])
            result = client._request("POST", "/v2/search/people", body)
        else:
            result = client.people_search(job_title_include=["CEO"], company_country_code=["GB"], max_results=3)
        print(json.dumps(result, indent=2))

    elif cmd == "employee-finder":
        url = sys.argv[2] if len(sys.argv) > 2 else "https://www.linkedin.com/company/blitz-api"
        result = client.employee_finder(url, max_results=5)
        print(json.dumps(result, indent=2))

    elif cmd == "waterfall-icp":
        url = sys.argv[2] if len(sys.argv) > 2 else "https://www.linkedin.com/company/blitz-api"
        cascade = [
            {"include_title": ["CEO", "Founder", "Owner"], "location": ["WORLD"]},
            {"include_title": ["Director", "VP", "Head"], "location": ["WORLD"]}
        ]
        result = client.waterfall_icp_search(url, cascade, max_results=3)
        print(json.dumps(result, indent=2))

    elif cmd == "find-email":
        url = sys.argv[2] if len(sys.argv) > 2 else "https://www.linkedin.com/in/antoine-blitz-5581b7373"
        result = client.find_work_email(url)
        print(json.dumps(result, indent=2))

    elif cmd == "company-enrich":
        url = sys.argv[2] if len(sys.argv) > 2 else "https://www.linkedin.com/company/blitz-api"
        result = client.company_enrichment(url)
        print(json.dumps(result, indent=2))

    elif cmd == "domain-to-linkedin":
        domain = sys.argv[2] if len(sys.argv) > 2 else "https://www.blitz-api.ai"
        result = client.domain_to_linkedin(domain)
        print(json.dumps(result, indent=2))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
