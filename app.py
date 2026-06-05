from flask import Flask, render_template, request, jsonify
from google import genai
import json
import os
import re
from datetime import datetime, timedelta
import threading

app = Flask(__name__)

# Default placeholder (will be overridden by .env or environment)
DEFAULT_GEMINI_API_KEY = 'YOUR_API_KEY'
VALID_PLACEHOLDER_KEYS = {'API_key_Goes_here', 'YOUR_REAL_API_KEY_HERE', ''}

def load_local_env(path='.env'):
    env = {}
    if not os.path.exists(path):
        return env
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        pass
    return env

local_env = load_local_env()

def is_valid_api_key(key):
    return bool(key) and key not in VALID_PLACEHOLDER_KEYS

def get_api_key():
    candidates = [
        os.getenv('GEMINI_API_KEY'),
        os.getenv('GOOGLE_API_KEY'),
        local_env.get('GEMINI_API_KEY'),
        local_env.get('GOOGLE_API_KEY'),
        DEFAULT_GEMINI_API_KEY,
    ]
    for candidate in candidates:
        if is_valid_api_key(candidate):
            return candidate
    return None

my_api_key = get_api_key()
client = None
if my_api_key:
    try:
        client = genai.Client(api_key=my_api_key)
        print('Gemini client configured.')
    except Exception as e:
        client = None
        print('Gemini init failed:', str(e))
else:
    client = None
    print('No valid Gemini API key configured.')

# --- Rate limiter ---
class RateLimiter:
    def __init__(self):
        self.locks = threading.Lock()
        self.requests = []
        self.daily_requests = 0
        self.last_reset = datetime.now()

    def can_make_request(self):
        now = datetime.now()
        if now.date() > self.last_reset.date():
            with self.locks:
                self.daily_requests = 0
                self.last_reset = now
        minute_ago = now - timedelta(minutes=1)
        self.requests = [req for req in self.requests if req > minute_ago]
        return not (len(self.requests) >= 15 or self.daily_requests >= 1500)

    def add_request(self):
        with self.locks:
            self.requests.append(datetime.now())
            self.daily_requests += 1

rate_limiter = RateLimiter()

# --- Robust JSON extraction ---
def extract_json_from_response(text):
    if not text:
        return {"professions": []}
    # Try direct parse
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    # Try to find the first JSON object in the text
    try:
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            candidate = match.group()
            try:
                return json.loads(candidate)
            except Exception:
                # Try to clean trailing commas
                cleaned = re.sub(r',\s*([\]}])', r'\1', candidate)
                try:
                    return json.loads(cleaned)
                except Exception:
                    pass
    except Exception:
        pass
    # Fallback empty professions
    return {"professions": []}

# --- Fallback suggestions when Gemini not available ---
def generate_fallback_career_suggestions(data):
    return {
        "professions": [
            {
                "name": "Software Developer",
                "requiredSkills": ["Problem solving", "Programming", "Teamwork"],
                "careerPath": ["10th Grade - Focus on Maths", "12th Grade - STEM", "Bachelor's in Computer Science", "Entry-level Developer", "Career progression"],
                "salaryRange": "₹5,00,000 - ₹12,00,000",
                "marketStats": "High demand for software roles globally.",
                "successStory": "Built apps that help businesses automate tasks."
            },
            {
                "name": "Data Analyst",
                "requiredSkills": ["Data visualization", "Statistics", "Excel"],
                "careerPath": ["10th Grade - Focus on Maths", "12th Grade - Commerce/Science", "Bachelor's in Statistics or Commerce", "Entry-level Analyst", "Career progression"],
                "salaryRange": "₹4,50,000 - ₹10,00,000",
                "marketStats": "Data-driven decisions are a top priority.",
                "successStory": "Helped a startup improve sales by analyzing trends."
            },
            {
                "name": "Digital Marketer",
                "requiredSkills": ["Communication", "Creativity", "Social media"],
                "careerPath": ["10th Grade - English and Communication", "12th Grade - Arts/Commerce", "Bachelor's in Marketing", "Entry-level Marketing Executive", "Career progression"],
                "salaryRange": "₹4,00,000 - ₹9,00,000",
                "marketStats": "Online presence is essential for modern businesses.",
                "successStory": "Increased brand visibility using digital campaigns."
            }
        ],
        "note": "Fallback suggestions are shown because the Gemini API key or response parsing failed."
    }

# --- Improved grade-level filtering: start career path at student's current level ---
def filter_career_path(career_path, grade_level):
    if not isinstance(career_path, list):
        return career_path

    grade_level = (grade_level or "").strip().lower()

    # Map grade level keywords to canonical start stages
    start_stage_map = {
        "10": "10th",
        "10th": "10th",
        "class 10": "10th",
        "class10": "10th",
        "12": "12th",
        "12th": "12th",
        "class 12": "12th",
        "class12": "12th",
        "undergraduate": "bachelor",
        "bachelor": "bachelor",
        "b.tech": "bachelor",
        "bsc": "bachelor",
        "ba": "bachelor",
        "postgraduate": "master",
        "master": "master",
        "m.tech": "master",
        "msc": "master"
    }

    start_stage = None
    for key, stage in start_stage_map.items():
        if key in grade_level:
            start_stage = stage
            break

    # If no recognizable grade level, return original path
    if not start_stage:
        return career_path

    filtered = []
    start_found = False
    for step in career_path:
        step_lower = (step or "").lower()
        # If the step mentions the start stage, begin including from there
        if not start_found:
            if start_stage in step_lower:
                start_found = True
                filtered.append(step)
        else:
            filtered.append(step)

    # If nothing matched (e.g., career_path uses different wording), try heuristic:
    if not filtered:
        # If start_stage is bachelor or master, drop 10th/12th steps heuristically
        if start_stage in ("bachelor", "master"):
            for step in career_path:
                if not any(k in (step or "").lower() for k in ["10th", "12th"]):
                    filtered.append(step)
        else:
            filtered = career_path

    return filtered if filtered else career_path

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/analyze', methods=['POST'])
def analyze_profile():
    if not rate_limiter.can_make_request():
        return jsonify({'error': 'Rate limit exceeded. Please try again later.'}), 429

    data = request.json or {}
    rate_limiter.add_request()

    # If no Gemini client, return fallback suggestions (filtered)
    if client is None:
        suggestions = generate_fallback_career_suggestions(data)
        grade_level = data.get("gradeLevel", "")
        for profession in suggestions.get("professions", []):
            profession["careerPath"] = filter_career_path(profession.get("careerPath", []), grade_level)
        return jsonify(suggestions)

    try:
        # Strong instruction to return only valid JSON in the exact format
        prompt = f"""
Act as a career counselor analyzing a student's profile. Based on the following information, suggest suitable career paths.

Goals: {data.get('goals')}
Interests: {data.get('interests')}
Current Skills: {data.get('currentSkills')}

Respond ONLY with valid JSON in this exact format (no extra text, no explanation):

{{
  "professions": [
    {{
      "name": "Profession Name",
      "requiredSkills": ["skill1", "skill2", "skill3"],
      "careerPath": ["10th Grade - ...", "12th Grade - ...", "Bachelor's - ...", "Master's - ...", "Entry-level - ...", "Career progression - ..."],
      "salaryRange": "Salary range in INR",
      "marketStats": "Job market outlook",
      "successStory": "Brief success story"
    }},
    {{ /* second profession */ }},
    {{ /* third profession */ }}
  ]
}}

Important:
1. Provide exactly 3 professions.
2. Ensure all fields are present for each profession.
3. Do not include any text outside the JSON object.
"""

        response = client.models.generate_content(
            model='gemini-3.5-flash',
            contents=prompt
        )

        # response.text may be a method or property depending on SDK; handle both
        raw_text = ""
        try:
            raw_text = response.text
            if callable(raw_text):
                raw_text = raw_text()
        except Exception:
            try:
                raw_text = str(response)
            except Exception:
                raw_text = ""

        career_data = extract_json_from_response(raw_text)

        # Ensure professions is a list
        if not isinstance(career_data.get("professions"), list):
            career_data = {"professions": []}

        # Apply grade-level filtering
        grade_level = data.get("gradeLevel", "")
        for profession in career_data.get("professions", []):
            profession["careerPath"] = filter_career_path(profession.get("careerPath", []), grade_level)

        # If still empty, return fallback
        if not career_data.get("professions"):
            suggestions = generate_fallback_career_suggestions(data)
            for profession in suggestions.get("professions", []):
                profession["careerPath"] = filter_career_path(profession.get("careerPath", []), grade_level)
            return jsonify(suggestions)

        return jsonify(career_data)

    except Exception as e:
        # On any error, return fallback with error details suppressed for UI
        suggestions = generate_fallback_career_suggestions(data)
        grade_level = data.get("gradeLevel", "")
        for profession in suggestions.get("professions", []):
            profession["careerPath"] = filter_career_path(profession.get("careerPath", []), grade_level)
        return jsonify(suggestions)

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)
