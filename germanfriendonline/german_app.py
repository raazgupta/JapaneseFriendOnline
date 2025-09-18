from flask import Flask, render_template, request, session, redirect, url_for, jsonify, flash, has_request_context
from flask_session import Session
from openai import OpenAI
import os
import random
from datetime import datetime, timedelta
import json
import unicodedata
from threading import Thread
import time
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = os.getenv('FLASK_SESSION_SECRET_KEY')

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Use server-side session storage
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = os.path.join(os.getcwd(), 'flask_session_data')
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_USE_SIGNER'] = True
Session(app)

# Store background results (use Redis or DB in production)
story_results = {}  # Dict to hold story by session ID or custom token
# Background sentences generation for Anki (Responses API)
anki_sentences_jobs = {}  # { session_id: {status: 'in_progress'|'done'|'error', response: str, error: str} }

# Background prefetch state for Anki translations
# Keyed by f"{session_id}:{card_number}" and stores statuses/results
anki_translation_jobs = {}

# ------------------------------------------------------------------------------
# 1) constants and helper
# ------------------------------------------------------------------------------
DEFAULT_WORTLIST_FILE = "A1Wortlist.csv"

# Assistant IDs no longer used after migration to Responses API

def get_current_wortlist_file():
    # fall back to default if none selected
    if has_request_context():
        print(f"Using Wortlist: {session.get('wortlist_file', DEFAULT_WORTLIST_FILE)}")
    return session.get("wortlist_file", DEFAULT_WORTLIST_FILE)

def generate_story_background(session_key, wortlist_file, scenario_text):
    burned_words = get_burned_words(wortlist_file)
    # Initialize status so the page can render a placeholder immediately
    story_results[session_key] = {
        'german_status': 'in_progress',
        'english_status': 'pending',
        'german': '',
        'english': ''
    }

    if not burned_words:
        story_results[session_key] = {
            'german_status': 'done',
            'english_status': 'done',
            'german': "No burned words available yet. Please review and burn more words first.",
            'english': "No burned words available yet. Please review and burn more words first."
        }
        return

    # Build a concise prompt for faster response
    prompt = f"""
            Write an interesting German story for this scenario: {scenario_text}.
            Can use any Proper Nouns including those that are in the Scenario text such as 'Raj'. 
            Write the story primarily using Nouns, Verbs and Adjectives that are in this list: {', '.join(burned_words)}.
            """
    messages = [
        {'role': 'system', 'content': 'You are a helpful language teacher.'},
        {'role': 'user', 'content': prompt}
    ]

    try:
        # Use a lighter model and lower reasoning to speed up German story
        german_story = get_completion_from_messages(messages, model="gpt-5", max_tokens=None, reasoning_effort="medium")
        german_story = german_story.strip()
        story_results[session_key]['german'] = german_story
        story_results[session_key]['german_status'] = 'done'

        # Kick off English translation in a separate thread so the page can update later
        Thread(target=generate_english_translation, args=(session_key,), daemon=True).start()
    except Exception as e:
        story_results[session_key]['german'] = f"Error generating story: {e}"
        story_results[session_key]['german_status'] = 'error'


def generate_english_translation(session_key: str):
    # Translate the generated German story to English
    result = story_results.get(session_key)
    if not result or not result.get('german'):
        return
    story_results[session_key]['english_status'] = 'in_progress'
    try:
        messages = [
            {'role': 'system', 'content': 'You are a helpful language teacher.'},
            {'role': 'user', 'content': 'Translate this German story to English.'},
            {'role': 'assistant', 'content': result['german']}
        ]
        english_story = get_completion_from_messages(messages, model="gpt-5-mini", max_tokens=None)
        story_results[session_key]['english'] = english_story.strip()
        story_results[session_key]['english_status'] = 'done'
    except Exception as e:
        story_results[session_key]['english'] = f"Error translating story: {e}"
        story_results[session_key]['english_status'] = 'error'



# If reviewing A1Wortlist, the burned worts are from A1 list
# If reviewing A2Wortlist, then burned worts are from A1 and A2 list.
# Once A2 bunred wort list is large enough (>1000 words) can consider switching to exclusively A2 list.
def get_burned_words(wortlist_file):
    burned_words = []

    file_path = wortlist_file

    files_to_read = []
    if file_path == "A2Wortlist.csv":
        files_to_read = ["A1Wortlist.csv", "A2Wortlist.csv"]
    else:
        files_to_read = [file_path]

    for path in files_to_read:
        try:
            with open(path, 'r') as file:
                for lineNumber, line in enumerate(file):
                    if lineNumber == 0:
                        continue  # Skip header
                    line = line.strip()
                    elements = line.split(',')
                    if len(elements) >= 2:
                        word, frequency = elements[0], elements[1]
                        if frequency == "B":
                            burned_words.append(word)
        except FileNotFoundError:
            print(f"File not found: {path}")

    # Shuffle to ensure random order each time
    random.shuffle(burned_words)

    return burned_words



def get_completion_from_messages(messages, model="gpt-5-nano", max_tokens=2000, reasoning_effort="minimal", verbosity=None):
    """
    messages: [{'role':'system','content':'...'}, {'role':'user','content':'...'}, ...]
    reasoning_effort: "low", "medium", or "high"
    """
    create_args = {
        "model": model,
        "input": messages,
        "reasoning": {"effort": reasoning_effort},
    }
    if max_tokens is not None:
        create_args["max_output_tokens"] = max_tokens
    if verbosity is not None:
        create_args["text"] = {"verbosity": verbosity}
    resp = client.responses.create(**create_args)

    return resp.output_text

def get_selected_level():
    """Return 'A1' or 'A2' based on the user's wortlist choice."""
    file_path = get_current_wortlist_file()
    return 'A2' if file_path == 'A2Wortlist.csv' else 'A1'

def _get_session_id():
    try:
        return session.sid
    except Exception:
        # Fallback if Flask-Session sid is unavailable
        return request.cookies.get(app.session_cookie_name, "unknown")

def _anki_job_key(card_number: int, wort: str = None):
    base = f"{_get_session_id()}:{card_number}"
    if wort:
        try:
            w = unicodedata.normalize('NFC', str(wort)).lower()
        except Exception:
            w = str(wort)
        base = f"{base}:{w}"
    return base

## Removed JSON parse fallback for sentence resolution; rely on session['anki_sentence']

def _start_anki_prefetch(card_number: int, wort: str, german_sentence: str):
    """Start background tasks to fetch: (1) one-word translation, (2) English sentence translation.
    Stores progress/results in anki_translation_jobs.
    """
    if card_number is None or not wort:
        return False

    key = _anki_job_key(card_number, wort)
    # If an existing job for this card exists and is in progress or done, don't restart
    existing = anki_translation_jobs.get(key)
    if existing:
        # If it's for the same word and already running/done, keep it; otherwise start fresh
        if existing.get('word') == wort and (
            existing.get('word_status') in ('in_progress', 'done') or existing.get('sentence_status') in ('in_progress', 'done')
        ):
            return True

    anki_translation_jobs[key] = {
        'word': wort,
        'german_sentence': german_sentence or '',
        'word_translation': None,
        'word_status': 'in_progress' if wort else 'error',
        'sentence_translation': None,
        'sentence_status': 'in_progress',
        'created_at': datetime.utcnow().isoformat()
    }

    def compute_word():
        try:
            messages = [
                {'role': 'system', 'content': 'Respond with a single English word only. No sentences or explanations.'},
                {'role': 'user', 'content': 'One Word English translation for: Klima'},
                {'role': 'assistant', 'content': 'Climate'},
                {'role': 'user', 'content': f'One Word English translation for: {wort}'},
            ]
            # Remove max_output_tokens (use model default) and set verbosity low for concise output
            resp = get_completion_from_messages(messages, model="gpt-5-nano", max_tokens=None, verbosity="low")
            anki_translation_jobs[key]['word_translation'] = resp.strip()
            anki_translation_jobs[key]['word_status'] = 'done'
        except Exception as e:
            anki_translation_jobs[key]['word_translation'] = f"Error: {e}"
            anki_translation_jobs[key]['word_status'] = 'error'

    def compute_sentence():
        try:
            # Use german sentence captured at job creation; avoid accessing Flask session in background threads
            gs = german_sentence or anki_translation_jobs.get(key, {}).get('german_sentence')
            if not gs:
                raise ValueError('No German sentence found for this word')
            resp = translateToEnglish(gs)
            anki_translation_jobs[key]['sentence_translation'] = resp.strip()
            anki_translation_jobs[key]['sentence_status'] = 'done'
        except Exception as e:
            anki_translation_jobs[key]['sentence_translation'] = f"Error: {e}"
            anki_translation_jobs[key]['sentence_status'] = 'error'

    # Run in background threads
    Thread(target=compute_word, daemon=True).start()
    Thread(target=compute_sentence, daemon=True).start()
    return True

## Removed: Assistants API helpers (migrated to Responses API)


def chooseSelectedWords():
    # Go through the file
    # Add 10 words with today's date or earlier to selected words as array: [word, line number in wortLines]
    # Also in the case that 10 words are not found, keep an array of [word,line number] that do not have reviewData
    # Then check for the number of not found words,
    # and choose that number from the array in random and add to the selected words array

    # Format [Word, LineNumber, ReviewFrequency, ReviewDateString]
    selected_words_lineNumber = []


    not_reviewed_words = []
    number_burned = 0
    number_week = 0
    number_month = 0
    number_3_month = 0
    number_pending = 0
    number_tomorrow = 0
    total_lines = 0

    file_path = get_current_wortlist_file()

    with open(file_path, 'r') as file:
        # num_lines = sum(1 for line in file)
        # print(f"num_lines: {num_lines}")
        for lineNumber, line in enumerate(file, start=0):
            line = line.strip()
            lineElements = line.split(',')
            if lineNumber >= 1:
                word = lineElements[0]
                reviewFrequency = lineElements[1]
                reviewDateString = lineElements[2]

                if reviewDateString == "":
                    not_reviewed_words.append([word, lineNumber, reviewFrequency, reviewDateString])
                    number_pending = number_pending + 1
                elif reviewFrequency != "B":
                    reviewDateObject = datetime.strptime(reviewDateString, "%Y-%m-%d").date()
                    today = datetime.now().date()
                    if reviewDateObject <= today and len(selected_words_lineNumber) < 10:
                        selected_words_lineNumber.append([word, lineNumber, reviewFrequency, reviewDateString])

                    if reviewFrequency == "W":
                        number_week = number_week + 1
                    elif reviewFrequency == "M":
                        number_month = number_month + 1
                    elif reviewFrequency == "3M":
                        number_3_month = number_3_month + 1
                    elif reviewFrequency == "T":
                        number_tomorrow = number_tomorrow + 1

                elif reviewFrequency == "B":
                    number_burned = number_burned + 1
                total_lines = total_lines + 1

    # print("Number of selected words using reviewDate:", len(selected_words_lineNumber))

    # Check if selected_words has 10 orders or add random words to it
    num_selected_words = len(selected_words_lineNumber)
    if num_selected_words < 10:
        num_missing = 10 - num_selected_words
        if num_missing <= len(not_reviewed_words):
            random_word_indices = random.sample(range(0, len(not_reviewed_words)), num_missing)
        else:
            random_word_indices = random.sample(range(0, len(not_reviewed_words)), len(not_reviewed_words))
        for random_index in random_word_indices:
            selected_words_lineNumber.append(not_reviewed_words[random_index])

    # print("Number of selected words after adding random:", len(selected_words_lineNumber))

    # Create a list of selected words
    selected_words = []
    for selected_word_lineNumber in selected_words_lineNumber:
        selected_words.append(selected_word_lineNumber[0])
    random.shuffle(selected_words)

    # percentage_burned = number_burned / total_lines

    return selected_words_lineNumber, selected_words, number_burned, number_week, number_month, number_3_month, number_pending, number_tomorrow

def create_anki_english_sentences(selected_words):
    # Function to get Anki sentences in 1 go in JSON format using Responses API.
    # Starts a background task and does not wait for completion
    session_key = session.sid
    anki_sentences_jobs[session_key] = {'status': 'in_progress'}

    # Capture level while inside request context; do not access session in thread
    level = get_selected_level()

    def task(level_param):
        prompt = f"""
            You are creating example sentences for vocabulary review at level {level_param}.
            1) For each of these words, write exactly one simple, natural German sentence: {', '.join(selected_words)}.
            2) Constraint: Use only nouns, verbs and adjectives that are part of the Goethe-Zertifikat {level_param} vocabulary list. Do not use any noun or verb that is outside this list.
               Function words (articles, pronouns, prepositions, conjunctions) are allowed as needed.
            3) Keep grammar and vocabulary appropriate for level {level_param}.
            4) Output must be pure JSON (no markdown fences), with each key as the word and the value as its German sentence.
            Example only: {{"Word1": "German sentence for Word1"}}
        """
        messages = [
            {'role': 'system', 'content': 'You are a helpful language teacher.'},
            {'role': 'user', 'content': prompt}
        ]
        try:
            resp = get_completion_from_messages(messages, model="gpt-5-mini", max_tokens=2000)
            cleaned = resp.strip()
            # Clean potential code fences or leading 'json'
            if cleaned.lower().startswith('json'):
                cleaned = cleaned[4:].strip()
            cleaned = cleaned.strip('`')
            anki_sentences_jobs[session_key] = {'status': 'done', 'response': cleaned}
        except Exception as e:
            anki_sentences_jobs[session_key] = {'status': 'error', 'error': str(e)}

    Thread(target=task, args=(level,), daemon=True).start()

def save_to_csv():

    updated_content = []
    file_path = get_current_wortlist_file()
    with open(file_path, 'r') as file:
        lines = file.readlines()

    # Modify the rows that have updated frequency and review date
    selected_words_lineNumber = session['selected_words_lineNumber']
    rows_to_update = []
    for selected_word_LineNumber in selected_words_lineNumber:
        rows_to_update.append(selected_word_LineNumber[1])

    for i, line in enumerate(lines, start=0):
        if i in rows_to_update:
            updated_line = ""
            for selected_word_LineNumber in selected_words_lineNumber:
                if selected_word_LineNumber[1] == i:
                    updated_line = selected_word_LineNumber[0] + "," + selected_word_LineNumber[2] + ',' + selected_word_LineNumber[3] + "\n"
            updated_content.append(updated_line)
            #print(updated_line)
        else:
            updated_content.append(line)

    # Write data to CSV file
    #print(updated_content[0])
    with open(file_path, 'w') as file:
        file.writelines(updated_content)


@app.route('/story_scenario', methods=['POST'])
def story_scenario():
    # Save wortlist selection before showing storyScenario.html
    session['wortlist_file'] = request.form.get('wortlist', DEFAULT_WORTLIST_FILE)
    return render_template('storyScenario.html')


@app.route('/stats_and_start_anki', methods=['POST'])
def stats_and_start_anki():

    scenario_text = request.form.get('scenarioText', None)

    selected_words_lineNumber, selected_words, number_burned, number_week, number_month, number_3_month, number_pending, number_tomorrow = chooseSelectedWords()


    create_anki_english_sentences(selected_words)

    session['selected_words_position'] = 0
    session['selected_words_lineNumber'] = selected_words_lineNumber
    session['messages'] = []
    session['germanStory'] = ""

    # Start background story generation
    session_key = session.sid
    story_results[session_key] = {'status': 'in_progress'}
    wortlist_file = session.get("wortlist_file", DEFAULT_WORTLIST_FILE)
    Thread(target=generate_story_background, args=(session_key, wortlist_file, scenario_text)).start()

    # Get the last run datetime from the log file
    last_run_datetime = get_last_run_datetime()

    result_data = {
        'numberBurned': number_burned,
        'numberWeek': number_week,
        'numberMonth': number_month,
        'number3Month': number_3_month,
        'numberPending': number_pending,
        'numberTomorrow': number_tomorrow,
        'lastRunDateTime': last_run_datetime
    }

    # Save current run datetime in log file
    log_datetime()

    return render_template('stats_and_start_anki.html', result = result_data)

@app.route('/ankiSentencesResponse', methods=['POST','GET'])
def ankiSentencesResponse():
    # Block until the background job (Responses API) finishes, then return the JSON string
    session_key = session.sid
    # Wait loop similar to previous behavior
    while True:
        job = anki_sentences_jobs.get(session_key)
        if job and job.get('status') == 'done':
            response = job.get('response', '')
            session['anki_sentences'] = response
            return response
        if job and job.get('status') == 'error':
            session['anki_sentences'] = 'Error'
            return 'Error'
        time.sleep(0.5)


@app.route('/anki', methods=['POST','GET'])
def anki():
        # Guard against missing session state
        if 'selected_words_lineNumber' not in session or 'selected_words_position' not in session:
            print("[anki] Missing session keys; redirecting to index.")
            flash('Session expired — please restart practice', 'warning')
            return redirect(url_for('index'))

        selected_words_lineNumber = session['selected_words_lineNumber']
        selected_words_position = session['selected_words_position']

        wort = selected_words_lineNumber[selected_words_position][0]
        session['anki_word'] = wort
        # Track current card number for polling/prefetch keying
        number = selected_words_position + 1
        session['current_anki_number'] = number

        result_data = {
            'wort': wort,
            'number': number
        }

        return render_template('anki.html', result = result_data)


@app.route('/ankiSentence')
def ankiSentence():

    # print("anki_word:" + session['anki_word'])

    wort = session['anki_word']
    anki_sentences = session['anki_sentences']
    anki_sentence_for_wort = "Error"
    sentences_data = ""

    try:
        # print("anki_sentences:" + anki_sentences)
        sentences_data = json.loads(anki_sentences)
    except json.JSONDecodeError:
        anki_sentence_for_wort = "Failed to parse JSON. Please check the JSON structure"
    # Only attempt lookup if JSON was parsed into a dict
    if isinstance(sentences_data, dict):
        try:
            wort_normalized = unicodedata.normalize('NFC', wort)
            sentences_data_normalized = {unicodedata.normalize('NFC', k): v for k, v in sentences_data.items()}
            anki_sentence_for_wort = sentences_data_normalized.get(wort_normalized)
        except Exception:
            anki_sentence_for_wort = 'Failed to get word. Please check if word is in the JSON string'

    session['anki_sentence'] = anki_sentence_for_wort
    # print("anki_sentence_for_wort:" + anki_sentence_for_wort)

    return anki_sentence_for_wort

@app.route('/ankiTranslate', methods=['POST'])
def anki_translate():
    # Guard against missing session (expired or direct navigation)
    if 'selected_words_lineNumber' not in session or 'selected_words_position' not in session:
        # Minimal recovery: send user back to start to rebuild session
        print("[anki_translate] Missing session keys; redirecting to index.")
        flash('Session expired — please restart practice', 'warning')
        return redirect(url_for('index'))

    selected_words_lineNumber = session['selected_words_lineNumber']
    selected_words_position = session['selected_words_position']

    wort = selected_words_lineNumber[selected_words_position][0]
    final_word = 0
    number = selected_words_position + 1

    if (selected_words_position + 1) == len(selected_words_lineNumber):
        final_word = 1

    # Try to use prefetch results if available; otherwise, show placeholder and poll on page
    # Ensure current card number is tracked
    session['current_anki_number'] = number

    # Prepare base result
    result_data = {
        'translation': 'thinking...',
        'sentence_translation': 'thinking...',
        'frequency1': '',
        'frequency2': '',
        'frequency1_display': '',
        'frequency2_display': '',
        'final_word': final_word,
        'number': number
    }

    # Fill from prefetch if ready; if job absent, start it now in background
    key = _anki_job_key(number, wort)
    job = anki_translation_jobs.get(key)
    if not job:
        # Source German sentence from session only
        german_sentence = session.get('anki_sentence')
        _start_anki_prefetch(number, wort, german_sentence)
        job = anki_translation_jobs.get(key)
    if job and job.get('word_status') == 'done' and job.get('word_translation'):
        result_data['translation'] = job['word_translation']
    if job and job.get('sentence_status') == 'done' and job.get('sentence_translation'):
        result_data['sentence_translation'] = job['sentence_translation']

    # Request for next Frequency
    currentFrequency = selected_words_lineNumber[selected_words_position][2]
    if currentFrequency == "" or currentFrequency == "T":
        result_data['frequency1'] = 'T'
        result_data['frequency2'] = 'W'
    elif currentFrequency == "W":
        result_data['frequency1'] = 'T'
        result_data['frequency2'] = 'M'
    elif currentFrequency == "M":
        result_data['frequency1'] = 'T'
        result_data['frequency2'] = '3M'
    elif currentFrequency == "3M":
        result_data['frequency1'] = 'T'
        result_data['frequency2'] = 'B'
    elif currentFrequency == "B":
        result_data['frequency1'] = 'B'
        result_data['frequency2'] = 'B'

    def get_display_frequency(frequency_code):
        if frequency_code == 'T':
            return 'Tomorrow'
        elif frequency_code == 'W':
            return 'Week'
        elif frequency_code == 'M':
            return 'Month'
        elif frequency_code == '3M':
            return '3 Months'
        elif frequency_code == 'B':
            return 'Burned'
        else:
            return 'Unknown'

    result_data['frequency1_display'] = get_display_frequency(result_data['frequency1'])
    result_data['frequency2_display'] = get_display_frequency(result_data['frequency2'])

    return render_template('ankiTranslate.html', result=result_data)

@app.route('/ankiSentenceEnglish')
def ankiSentenceEnglish():
   anki_sentence = session['anki_sentence']
   anki_sentence_english = translateToEnglish(anki_sentence)
   return anki_sentence_english


@app.route('/anki_prefetch', methods=['POST'])
def anki_prefetch():
    """Starts background prefetch of translations for the current card.
    Called by anki.html after the German sentence is shown.
    """
    selected_words_lineNumber = session.get('selected_words_lineNumber')
    selected_words_position = session.get('selected_words_position', 0)
    if not selected_words_lineNumber:
        return jsonify({'ok': False, 'error': 'Session expired'}), 400

    wort = selected_words_lineNumber[selected_words_position][0]
    number = selected_words_position + 1
    session['anki_word'] = wort
    session['current_anki_number'] = number

    # Find German sentence for this wort from session only
    german_sentence = session.get('anki_sentence')
    if not german_sentence:
        return jsonify({'ok': False, 'error': 'No sentence available in session'}), 400

    started = _start_anki_prefetch(number, wort, german_sentence)
    return jsonify({'ok': started})


@app.route('/anki_poll', methods=['GET'])
def anki_poll():
    """Poll current card prefetch status/results for word and sentence translations."""
    selected_words_position = session.get('selected_words_position', 0)
    number = session.get('current_anki_number', selected_words_position + 1)
    key = _anki_job_key(number, session.get('anki_word'))
    job = anki_translation_jobs.get(key)
    if not job:
        return jsonify({
            'word_status': 'pending',
            'sentence_status': 'pending'
        })

    payload = {
        'word_status': job.get('word_status', 'pending'),
        'sentence_status': job.get('sentence_status', 'pending')
    }
    if job.get('word_status') in ('done','error'):
        payload['word_translation'] = job.get('word_translation', '')
    if job.get('sentence_status') in ('done','error'):
        payload['sentence_translation'] = job.get('sentence_translation', '')
    return jsonify(payload)

def translateToEnglish(germanText):
    messages = [
        {'role': 'system',
         'content': f"""
                    You are given text in German. Translate it to English. 
                 """
         },
        {'role': 'user',
         'content': germanText
         }
    ]

    englishVersion = get_completion_from_messages(messages, model="gpt-5-nano", max_tokens=100)

    return englishVersion

def correctSpellingGrammar(germanText):
    messages = [
        {'role': 'system',
         'content': f"""
                        Read this German text and fix any spelling and grammar errors.
                        If there are no errors then respond back with the same German text.
                     """
         },
        {'role': 'user',
         'content': germanText
         }
    ]
    # print("correctSpellingGrammar:")
    # print(messages)
    correctSpellingGrammarVersion = get_completion_from_messages(messages, model="gpt-5-mini", max_tokens=500)
    # print(correctSpellingGrammarVersion)

    return correctSpellingGrammarVersion

@app.route('/german_story_status', methods=['GET'])
def german_story_status():
    # Backward-compatible: return a composite status mainly for legacy polling
    result = story_results.get(session.sid)
    if not result:
        return jsonify({'status': 'expired'})
    # Derive a combined status
    if result.get('german_status') == 'done' and result.get('english_status') == 'done':
        status = 'done'
    elif result.get('german_status') == 'error' or result.get('english_status') == 'error':
        status = 'error'
    else:
        status = 'in_progress'
    return jsonify({'status': status})

@app.route('/story_progress', methods=['GET'])
def story_progress():
    result = story_results.get(session.sid)
    if not result:
        return jsonify({'german_status': 'expired', 'english_status': 'expired'})
    payload = {
        'german_status': result.get('german_status', 'in_progress'),
        'english_status': result.get('english_status', 'pending')
    }
    if result.get('german_status') == 'done':
        payload['german'] = result.get('german', '')
    if result.get('english_status') == 'done':
        payload['english'] = result.get('english', '')
    return jsonify(payload)

@app.route('/german_story_with_translation', methods=['POST','GET'])
def german_story_with_translation():
    result = story_results.get(session.sid)
    german_text = ''
    english_text = ''
    if result:
        if result.get('german_status') == 'done':
            german_text = result.get('german', '')
        else:
            german_text = '🧠 Brewing a German story… this usually takes ~1–2 minutes. More time to think about how far you have come in your German language learning journey!'
        if result.get('english_status') == 'done':
            english_text = result.get('english', '')
        else:
            english_text = '🇬🇧 Translating to English… almost there!'
    else:
        german_text = '🧠 Brewing a German story… this usually takes ~1–2 minutes. More time to think about how far you have come in your German language learning journey!'
        english_text = ''

    return render_template('german_story_with_translation.html', result={
        'germanStory': german_text,
        'englishStory': english_text
    })

@app.route('/ankiRecord', methods=['POST'])
def updateReviewDate():
    # Based on next Frequency update the review date
    today = datetime.now().date()
    nextReviewDate = ""

    freq_input = request.form['frequency']

    if freq_input == "T":
        nextReviewDate = today + timedelta(days=1)
    elif freq_input == "W":
        nextReviewDate = today + timedelta(days=7)
    elif freq_input == "M":
        nextReviewDate = today + timedelta(days=30)
    elif freq_input == "3M":
        nextReviewDate = today + timedelta(days=90)
    elif freq_input == "B":
        nextReviewDate = today

    selected_words_lineNumber = session['selected_words_lineNumber']
    selected_words_position = session['selected_words_position']

    selected_words_lineNumber[selected_words_position][2] = freq_input
    selected_words_lineNumber[selected_words_position][3] = nextReviewDate.strftime("%Y-%m-%d")

    session['selected_words_position'] = selected_words_position + 1
    session['selected_words_lineNumber'] = selected_words_lineNumber

    if (selected_words_position + 1) < len(selected_words_lineNumber):
        # return redirect(url_for('anki', _external=False))
        return redirect('anki')
    else:
        # Update the Wortlist file with updated frequency and date
        save_to_csv()
        # Show the German Story with translation
        return redirect('german_story_with_translation')

@app.route('/germanConversation', methods=['POST'])
def germanConversation():
    # Save current run datetime in log file
    session['wortlist_file'] = request.form.get('wortlist', DEFAULT_WORTLIST_FILE)
    log_datetime()
    return render_template('germanConversation.html')

@app.route('/germanScenario', methods=['POST'])
def germanScenario():
    result_data = []

    scenarioText = request.form['scenarioText']

    # Initialize conversation using Responses API by storing messages in session
    level = get_selected_level()
    system_prompt = f"""
        {scenarioText}
        Guidelines:
        - Respond in German.
        - Use only nouns, verbs and adjectives that are in the Goethe-Zertifikat {level} vocabulary list.
        - Keep sentences simple and suitable for level {level}.
    """.strip()
    conversationMessages = [
        {'role': 'system', 'content': system_prompt}
    ]
    session['conversationMessages'] = conversationMessages

    return render_template('iSay.html', result=result_data)

@app.route('/iSayDynamic', methods=['POST'])
def iSayDynamic():
    session['iSayText'] = request.form['iSayText']
    iSayText = request.form['iSayText']

    conversationMessages = session.get('conversationMessages', [])
    if not conversationMessages:
        level = get_selected_level()
        conversationMessages = [{
            'role': 'system',
            'content': f'You are a helpful language teacher. Respond in German and use only nouns, verbs and adjectives from the Goethe-Zertifikat {level} vocabulary list. Keep sentences simple and level-appropriate ({level}).'
        }]

    # Append the user's message
    conversationMessages.append({'role': 'user', 'content': iSayText})

    # Get assistant reply via Responses API
    youSayText = get_completion_from_messages(conversationMessages, model="gpt-5-mini", max_tokens=400)

    # Update conversation history
    conversationMessages.append({'role': 'assistant', 'content': youSayText})
    session['conversationMessages'] = conversationMessages
    session['youSayText'] = youSayText

    result_data = {
        'youSayText': youSayText,
        'iSayText': iSayText
    }

    return render_template('youSayDynamic.html', result=result_data)

@app.route('/conversationEnglishTranslation')
def conversationEnglishTranslation():
    youSayText = session['youSayText']
    youSayTextEnglish = translateToEnglish(youSayText)
    return youSayTextEnglish

@app.route('/conversationSpellGrammarCheck')
def conversationSpellGrammarCheck():
    iSayText = session['iSayText']
    iSayTextReviewed = correctSpellingGrammar(iSayText)
    return iSayTextReviewed



@app.route('/youSay', methods=['POST'])
def youSay():
    return render_template('iSay.html')

def log_datetime():
    now = datetime.now()
    with open('datetime_log.txt', 'a') as log_file:
        log_file.write(now.strftime("%Y-%m-%d %H:%M:%S") + "\n")


def get_last_run_datetime():
    try:
        with open('datetime_log.txt', 'r') as log_file:
            lines = log_file.readlines()
            if lines:
                last_run_datetime = lines[-1].strip()
                return last_run_datetime
    except FileNotFoundError:
        return "No run history available."

@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    app.run(debug=False, port=5000)
