from flask import Flask, render_template, session, redirect, request, jsonify
from openai import OpenAI
import os
import random
from datetime import datetime
import requests
import time
from werkzeug.middleware.proxy_fix import ProxyFix
from threading import Thread
import uuid
import json
import re

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = os.environ.get('FLASK_SESSION_SECRET_KEY')

openai_client = None
burned_story_jobs = {}

BURNED_STORY_WAITING_MESSAGE = (
    "Deep breaths - your burned wisdom is weaving a new tale. "
    "Celebrate the kanji you have mastered while we compose the story. "
    "Please enjoy this mindful pause. Generation may take three minutes."
)

FURIGANA_WAITING_MESSAGE = (
    "Calm winds are guiding the readings into place. Your furigana will appear shortly."
)

ENGLISH_WAITING_MESSAGE = (
    "The translation is unfolding gently. Stay present while the English version arrives."
)


def ensure_openai_client():
    """Lazily initialize the OpenAI client so failure surfaces early with a clear error."""
    global openai_client
    if openai_client is None:
        api_key = os.environ.get('OPENAI_API_KEY')
        if not api_key:
            raise RuntimeError('OPENAI_API_KEY environment variable is not set.')
        openai_client = OpenAI(api_key=api_key)
    return openai_client


def get_completion_from_messages(messages, model="gpt-5-nano", max_tokens=2000, reasoning_effort="minimal", verbosity="low"):
    """Wrapper around OpenAI Responses API with optional chat completion fallback."""
    client = ensure_openai_client()

    create_args = {
        "model": model,
        "input": messages,
        "reasoning": {"effort": reasoning_effort},
        "max_output_tokens": max_tokens,
        "verbosity": verbosity,
    }
    response = client.responses.create(**create_args)

    if hasattr(response, 'output_text') and response.output_text:
        return response.output_text
    raise RuntimeError('No text returned from Responses API call.')

def get_response_from_wanikani(url_end = ""):
    if url_end.startswith("http"):
        api_url = url_end
    else:
        api_url = "https://api.wanikani.com/v2/" + url_end
    # print("WANIKANI API request:" + api_url )

    wanikani_api_key = os.environ.get('WANIKANI_API_KEY')
    custom_headers = {
        "Wanikani-Revision": "20170710",
        "Authorization": f"Bearer {wanikani_api_key}"
    }
    # print(custom_headers)
    response = requests.get(api_url, headers=custom_headers)

    if response.status_code == 200:
        data = response.json()
        #print("JSON response:")
        #print(data)
        return data
    else:
        print(f"Wanikani API Error: {response.status_code}")
        return None


def get_reasoning_completion(messages, model="gpt-5"):
    """Call the OpenAI GPT-5 reasoning model following the German app pattern."""
    client = ensure_openai_client()
    start_time = time.time()
    response = client.responses.create(
        model=model or "gpt-5",
        input=messages,
        reasoning={"effort": "medium"},
    )
    elapsed = time.time() - start_time
    #print(messages)
    print(f"Reasoning model response time: {elapsed:.2f} seconds.")
    if hasattr(response, 'output_text') and response.output_text:
        return response.output_text.strip()
    raise RuntimeError('No text returned from reasoning model response.')


def fetch_wanikani_assignments(subject_types, srs_stages):
    """Fetch all assignment records for the given subject types and SRS stages."""
    subject_types_param = ','.join(subject_types)
    srs_param = ','.join(str(stage) for stage in srs_stages)
    endpoint = f"assignments?subject_types={subject_types_param}&srs_stages={srs_param}"
    assignments = []
    next_url = endpoint
    while next_url:
        response_json = get_response_from_wanikani(next_url)
        if not response_json:
            break
        assignments.extend(response_json.get('data', []))
        next_url = response_json.get('pages', {}).get('next_url')
    return assignments


def fetch_wanikani_subjects(subject_ids):
    """Fetch subject details for the provided subject identifiers."""
    subjects = []
    if not subject_ids:
        return subjects
    chunk_size = 100
    for index in range(0, len(subject_ids), chunk_size):
        chunk = subject_ids[index:index + chunk_size]
        ids_param = ','.join(str(subject_id) for subject_id in chunk)
        response_json = get_response_from_wanikani(f"subjects?ids={ids_param}")
        if response_json:
            subjects.extend(response_json.get('data', []))
    return subjects


def gather_burned_word_lists():
    """Collect burned and almost burned vocabulary from WaniKani."""
    assignments = fetch_wanikani_assignments(['vocabulary', 'kana_vocabulary'], [8, 9])
    subject_ids = sorted({assignment['data']['subject_id'] for assignment in assignments})
    subjects = fetch_wanikani_subjects(subject_ids)
    burned_words = []
    for subject in subjects:
        data = subject.get('data', {})
        characters = data.get('characters') or data.get('slug')
        if characters:
            burned_words.append(characters)
    return burned_words


def generate_burned_story_text(burned_words):
    """Generate a Japanese story primarily using the provided burned words."""
    if not burned_words:
        raise RuntimeError('No burned vocabulary found in WaniKani data.')

    user_prompt = f"""
Write an interesting Japanese story.
Can use any Proper Nouns including those that are in the Scenario text such as 'Katya'. 
Write the story primarily using Nouns, Verbs and Adjectives that are in this list: {', '.join(burned_words)}.
Keep the story constrained to 3 paragraphs.
"""

    messages = [
        {'role': 'system', 'content': 'You are a helpful language teacher.'},
        {'role': 'user', 'content': user_prompt.strip()}
    ]

    story_text = get_reasoning_completion(messages)
    return story_text.strip()


def extract_json_object(text):
    text = (text or '').strip()
    if not text:
        raise ValueError('Empty response text.')
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    raise ValueError('Unable to parse JSON from response text.')


def generate_word_detail_via_model(word):
    messages = [
        {'role': 'system', 'content': 'You are a helpful Japanese language tutor.'},
        {'role': 'user', 'content': (
            "Provide the hiragana reading and a concise English meaning for the Japanese word "
            f"'{word}'. Respond strictly as JSON with keys 'hiragana' and 'english'. "
            "Use hiragana characters (no romaji) and keep the English to a short phrase."
        )}
    ]
    response_text = get_completion_from_messages(
        messages,
        model='gpt-5-nano',
        max_tokens=200,
        reasoning_effort='minimal',
        verbosity='low'
    )
    if not response_text:
        raise RuntimeError('No output text returned for word detail.')
    data = extract_json_object(response_text.strip())
    hiragana = data.get('hiragana', '').strip()
    english = data.get('english', '').strip()
    if not hiragana or not english:
        raise ValueError('Missing hiragana or english in response.')
    return hiragana, english


def _generate_word_detail(job_id, word):
    job = burned_story_jobs.get(job_id)
    if not job:
        return
    details = job['word_details'].get(word)
    if not details or details.get('status') not in {'in_progress', 'pending'}:
        return
    try:
        hiragana, english = generate_word_detail_via_model(word)
        details['hiragana'] = hiragana
        details['english'] = english
        details['status'] = 'done'
        details['error'] = None
        details['thread_started'] = False
    except Exception as exc:
        app.logger.exception('Word detail generation failed for %s', word)
        details['status'] = 'error'
        details['error'] = ''
        details['thread_started'] = False


def _generate_furigana_for_job(job_id):
    job = burned_story_jobs.get(job_id)
    if not job or job.get('story_status') != 'done':
        return
    try:
        html = withFuriganaHTMLParagraph(job.get('story', ''))
        job['furigana'] = html
        job['furigana_status'] = 'done'
        job['furigana_error'] = None
    except Exception as exc:
        app.logger.exception('Furigana generation failed.')
        job['furigana_status'] = 'error'
        job['furigana_error'] = str(exc)


def _generate_english_for_job(job_id):
    job = burned_story_jobs.get(job_id)
    if not job or job.get('story_status') != 'done':
        return
    try:
        english = translateToEnglish(job.get('story', ''))
        job['english'] = english
        job['english_status'] = 'done'
        job['english_error'] = None
    except Exception as exc:
        app.logger.exception('English translation failed.')
        job['english_status'] = 'error'
        job['english_error'] = str(exc)


def _run_burned_story_job(job_id):
    job = burned_story_jobs.get(job_id)
    if not job:
        return

    try:
        words = gather_burned_word_lists()
        job['words'] = words
        job['words_status'] = 'done'

        if not words:
            job['status'] = 'error'
            job['story_status'] = 'error'
            job['furigana_status'] = 'error'
            job['english_status'] = 'error'
            job['error'] = 'No burned vocabulary found yet. Please review and burn more words first.'
            job['furigana_error'] = job['error']
            job['english_error'] = job['error']
            return

        job['story_status'] = 'in_progress'
        story = generate_burned_story_text(words)
        job['story'] = story
        job['story_status'] = 'done'
        job['status'] = 'done'

        # Kick off furigana and English translations in background threads
        job['furigana_status'] = 'in_progress'
        Thread(target=_generate_furigana_for_job, args=(job_id,), daemon=True).start()

        job['english_status'] = 'in_progress'
        Thread(target=_generate_english_for_job, args=(job_id,), daemon=True).start()
    except Exception as exc:
        app.logger.exception('Burned story generation failed.')
        job['status'] = 'error'
        job['story_status'] = 'error'
        job['furigana_status'] = 'error'
        job['english_status'] = 'error'
        job['error'] = str(exc)
        job['furigana_error'] = str(exc)
        job['english_error'] = str(exc)


def start_burned_story_job():
    job_id = str(uuid.uuid4())
    burned_story_jobs[job_id] = {
        'status': 'in_progress',
        'words_status': 'in_progress',
        'words': [],
        'word_details': {},
        'story_status': 'pending',
        'story': '',
        'furigana_status': 'pending',
        'furigana': '',
        'furigana_error': None,
        'english_status': 'pending',
        'english': '',
        'english_error': None,
        'error': None,
    }
    Thread(target=_run_burned_story_job, args=(job_id,), daemon=True).start()
    return job_id


# Get assignments where levels=(1 to current level) and immediately available for review and subject_types=vocubulary
# This is a recursive function that goes up to Level 60, which is the max user level
def addVocabsInAscendingOrder(user_level, current_level_position, vocab_ids, max_words, num_levels_per_query):
    if user_level > current_level_position:
        levels_string = ""
        for counter in range(1,num_levels_per_query+1):
            current_level_position = current_level_position + 1
            if counter != num_levels_per_query:
                levels_string = levels_string + str(current_level_position) + ","
            else:
                levels_string = levels_string + str(current_level_position)
        url_end = f"assignments?levels={levels_string}&subject_types=vocabulary&immediately_available_for_review"
        assignments_json_dict = get_response_from_wanikani(url_end=url_end)
        if assignments_json_dict != None:
            current_vocab_ids = [item['data']['subject_id'] for item in assignments_json_dict['data']]
            number_missing = max_words - len(vocab_ids)
            if len(current_vocab_ids) > number_missing:
                current_vocab_ids = random.sample(current_vocab_ids, number_missing)
                vocab_ids.extend(current_vocab_ids)
                return vocab_ids
            else:
                vocab_ids.extend(current_vocab_ids)
                return addVocabsInAscendingOrder(user_level, current_level_position, vocab_ids, max_words, num_levels_per_query)
    else:
        return vocab_ids

def chooseSelectedWords(subject_types="vocabulary", max_words=5):
    # Format of selected_words [Word, Hiragana, Meaning]
    selected_words = []

    # Get Level of user
    user_json_dict = get_response_from_wanikani(url_end="user")
    if user_json_dict != None:
        user_level = user_json_dict['data']['level']

        if subject_types == "kanji":
            # Get assignments where levels=X and immediately available for review and subject_types=kanji
            url_end = f"assignments?levels={user_level}&subject_types={subject_types}&immediately_available_for_review"
            assignments_json_dict = get_response_from_wanikani(url_end=url_end)
            if assignments_json_dict != None:
                # From assignment, randomly select max_words kanji
                # Extract 'subject_id' values from the 'data' list
                kanji_ids = [item['data']['subject_id'] for item in assignments_json_dict['data']]
                # print(kanji_ids)
                if len(kanji_ids) > max_words:
                    kanji_ids = random.sample(kanji_ids, max_words)
                    # print(kanji_ids)
                for kanji_id in kanji_ids:
                    # Get the subject details
                    url_end = "subjects/" + str(kanji_id)
                    kanji_subject_json_dict = get_response_from_wanikani(url_end=url_end)
                    if kanji_subject_json_dict != None:
                        # For each kanji, ask ChatGPT to provide 1 simple comonly used word that has this Kanji, the hiragana reading, the meaning
                        kanji = kanji_subject_json_dict['data']['characters']
                        messages = [
                            {'role': 'system',
                             'content': """
                                Provide 1 simple commonly used word that has this Kanji
                                Also provide the Hiragana reading of this word.
                                Also provide the meaning of this word in English.
                                Output should be in comma separated format, like this: Kanji word, Hiragana reading, English translation.
                                """
                             },
                            {'role': 'user',
                             'content': "愛"
                             },
                            {'role': 'assistant',
                             'content': '愛してる,あいしてる,Love you'
                            },
                            {'role': 'user',
                             'content': f"{kanji}"
                             }
                        ]
                        response = get_completion_from_messages(messages = messages, max_tokens=100)
                        # print ("Kanji ChatGPT response:" + response)
                        kanji_components = response.split(',')
                        if len(kanji_components) == 3:
                            word = kanji_components[0]
                            hiragana = kanji_components[1]
                            meaning = kanji_components[2]
                            selected_words.append([word, hiragana, meaning])

        elif subject_types == "vocabulary":
            # Get assignments where levels=(1 to current level) and immediately available for review and subject_types=vocubulary
            num_levels_per_query = 5
            vocab_ids = addVocabsInAscendingOrder(user_level, 0, [], max_words, num_levels_per_query)

            for vocab_id in vocab_ids:
                url_end = "subjects/" + str(vocab_id)
                vocab_subject_json_dict = get_response_from_wanikani(url_end=url_end)
                if vocab_subject_json_dict != None:
                    word = vocab_subject_json_dict['data']['characters']
                    hiragana = vocab_subject_json_dict['data']['readings'][0]['reading']
                    meaning = vocab_subject_json_dict['data']['meanings'][0]['meaning']
                    selected_words.append([word, hiragana, meaning])

    #print (f"Selected Words: {selected_words}")
    return selected_words


def create_story(selected_words, max_sentences, temperature=0.0):
    # Prompt to create Japanese Story
    words = []
    for selected_word in selected_words:
        words.append(selected_word[0])

    messages = [
        {'role': 'system',
         'content': """
         You are a Japanese teacher. 
         """
         },
        {'role': 'user',
         'content': f"""
            Write a story in Japanese with maximum {max_sentences} sentences.
            The story should be written in simple Japanese that a child can understand. 
            Make sure these words are in the story: {",".join(words)}.
         """
         },
    ]

    # Print story in German
    response = get_completion_from_messages(messages, temperature=temperature, model="gpt-4", max_tokens=100)
    response = response.strip('"')
    # print("Japanese Story:")
    # print(response)

    messages.append(
        {'role': 'assistant',
         'content': response
         }
    )

    return messages, response


@app.route('/')
def index():

    return render_template('index.html')

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

@app.route('/japaneseStory', methods=['POST'])
def japaneseStory():

    # Maximum number of words per story
    max_words = 3
    # Maximum number of sentences per story
    max_sentences = 1

    # print(request.form.get('category'))
    selected_words = chooseSelectedWords(request.form.get('category'), max_words)
    # print(selected_words)
    temperature = (max_words - len(selected_words))/max_words
    messages, response = create_story(selected_words, max_sentences, temperature=temperature)

    session['selected_words_position'] = 0
    session['selected_words'] = selected_words
    session['messages'] = messages
    session['japaneseStory'] = response

    # Get the last run datetime from the log file
    last_run_datetime = get_last_run_datetime()

    result_data = {
        'japaneseStoryString': response,
        'lastRunDateTime': last_run_datetime
    }

    # Save current run datetime in log file
    log_datetime()

    return render_template('japaneseStory.html', result = result_data)


@app.route('/burnedStory', methods=['POST'])
def burned_story():
    job_id = start_burned_story_job()
    return render_template(
        'burnedStory.html',
        job_id=job_id,
        words=[],
        initial_story=BURNED_STORY_WAITING_MESSAGE,
        furigana_placeholder=FURIGANA_WAITING_MESSAGE,
        english_placeholder=ENGLISH_WAITING_MESSAGE
    )


@app.route('/burnedStory/status/<job_id>')
def burned_story_status(job_id):
    job = burned_story_jobs.get(job_id)
    if not job:
        return jsonify({'status': 'unknown'}), 404

    payload = {
        'status': job.get('status', 'in_progress'),
        'words_status': job.get('words_status'),
        'words': job.get('words', []),
        'word_details': job.get('word_details', {}),
        'story_status': job.get('story_status'),
        'story': job.get('story', ''),
        'furigana_status': job.get('furigana_status'),
        'furigana': job.get('furigana', ''),
        'english_status': job.get('english_status'),
        'english': job.get('english', ''),
        'error': job.get('error'),
        'furigana_error': job.get('furigana_error'),
        'english_error': job.get('english_error'),
    }
    return jsonify(payload)


@app.route('/burnedStory/word/<job_id>', methods=['POST'])
def burned_story_word_detail(job_id):
    job = burned_story_jobs.get(job_id)
    if not job:
        return jsonify({'status': 'unknown'}), 404

    data = request.get_json(silent=True) or {}
    word = data.get('word')
    if not word:
        return jsonify({'status': 'error', 'error': 'Missing word parameter.'}), 400

    if job.get('words_status') != 'done' or word not in job.get('words', []):
        return jsonify({'status': 'pending'}), 202

    details = job['word_details'].get(word)
    if not details:
        details = {
            'status': 'in_progress',
            'hiragana': '',
            'english': '',
            'error': None,
            'thread_started': True
        }
        job['word_details'][word] = details
        Thread(target=_generate_word_detail, args=(job_id, word), daemon=True).start()
    else:
        if details.get('status') == 'pending':
            details['status'] = 'in_progress'
        if details.get('status') == 'in_progress' and not details.get('thread_started'):
            details['thread_started'] = True
            Thread(target=_generate_word_detail, args=(job_id, word), daemon=True).start()

    response_payload = {
        'status': details.get('status'),
        'hiragana': details.get('hiragana', ''),
        'english': details.get('english', '')
    }
    return jsonify(response_payload)

@app.route('/anki', methods=['POST','GET'])
def anki():

        selected_words = session['selected_words']
        selected_words_position = session['selected_words_position']

        if len(selected_words) > selected_words_position:
            wort = selected_words[selected_words_position][0]
        else:
            wort = ""

        result_data = {
            'wort': wort,
            'number': selected_words_position + 1
        }

        return render_template('anki.html', result = result_data)


@app.route('/ankiTranslate', methods=['POST'])
def anki_translate():

    selected_words = session['selected_words']
    selected_words_position = session['selected_words_position']

    if len(selected_words) > selected_words_position:
        word = selected_words[selected_words_position][0]
        hiragana = selected_words[selected_words_position][1]
        translation = selected_words[selected_words_position][2]
        if len(selected_words) == (selected_words_position + 1):
            final_word = 1
        else:
            final_word = 0
    else:
        word = ""
        hiragana = ""
        translation = ""
        final_word = 0

    result_data = {
        'translation': translation,
        'hiragana': hiragana,
        'word': word,
        'number': selected_words_position + 1,
        'final_word': final_word
    }
    # print(result_data)

    return render_template('ankiTranslate.html', result=result_data)


def withFuriganaHTMLParagraph(japaneseStory):
    messages = [
        {'role': 'system',
         'content': f"""
             You are given a Japanese text. Convert it to an HTML paragraph. 
             Provide the Furigana characters above the Kanji characters using <ruby> blocks.
             The HTML paragraph should have font size of 30px. 
             """
         },
        {'role': 'user',
         'content': japaneseStory
         }
    ]

    '''
    {'role': 'user',
         'content': f"""
            ある日、学校の実験室で実験をしていたとき、友達がカメラで写真を撮りたいと言いました。私たちは入場券を求めて、写真を撮ることができました。写真を見ると、私たちの笑顔が広がっていました。
             """
         },
        {'role': 'assistant',
         'content': f"""
            <p style="font-size: 20px;">ある<ruby>日<rp>(</rp><rt>ひ</rt><rp>)</rp></ruby>、<ruby>森<rp>(</rp><rt>もり</rt><rp>)</rp></ruby>の<ruby>中<rp>(</rp><rt>ちゅう</rt><rp>)</rp></ruby>で<ruby>小<rp>(</rp><rt>ちいさ</rt><rp>)</rp></ruby>なウサギが<ruby>大<rp>(</rp><rt>おお</rt><rp>)</rp></ruby>きな<ruby>熊<rp>(</rp><rt>くま</rt><rp>)</rp></ruby>と<ruby>友達<rp>(</rp><rt>ともだち</rt><rp>)</rp></ruby>になりました。</p>
            """
         },
    '''

    furiganaVersion = get_completion_from_messages(messages, model="gpt-5", reasoning_effort="medium", max_tokens=10000)
    # print(furiganaVersion)
    return furiganaVersion

def translateToEnglish(japaneseStory):
    messages = [
        {'role': 'system',
         'content': f"""
                    You are given text in Japanese. Translate it to English. Make sure to translate all Japanese characters to English.
                 """
         },
        {'role': 'user',
         'content': japaneseStory
         }
    ]

    englishVersion = get_completion_from_messages(messages, model="gpt-5-nano")

    return englishVersion

def correctSpellingGrammar(japaneseStory):
    messages = [
        {'role': 'system',
         'content': f"""
                        Read this Japanese text and fix any spelling and grammar errors.
                        If there are no errors then respond back with the same Japanese text.
                     """
         },
        {'role': 'user',
         'content': japaneseStory
         }
    ]
    # print("correctSpellingGrammar:")
    # print(messages)
    correctSpellingGrammarVersion = get_completion_from_messages(messages, model="gpt-4", max_tokens=500)
    # print(correctSpellingGrammarVersion)

    return correctSpellingGrammarVersion

@app.route('/englishTranslationDynamic', methods=['POST','GET'])
def englishTranslationDynamic():
    japaneseStory = session['japaneseStory']
    result_data = {
        'japaneseStory': japaneseStory
    }
    return render_template('englishTranslationDynamic.html', result=result_data)

@app.route('/ankiEnglishTranslation')
def ankiEnglishTranslation():
    japaneseStory = session['japaneseStory']
    japaneseStoryEnglish = translateToEnglish(japaneseStory)
    return japaneseStoryEnglish

@app.route('/ankiFurigana')
def ankiFurigana():
    japaneseStory = session['japaneseStory']
    japaneseStoryFurigana = withFuriganaHTMLParagraph(japaneseStory)
    return japaneseStoryFurigana

@app.route('/englishTranslation', methods=['POST','GET'])
def englishTranslation():
    messages = session['messages']
    japaneseStory = session['japaneseStory']
    selected_words = session['selected_words']

    # Get the response with Furigana

    hiraganaStory = withFuriganaHTMLParagraph(japaneseStory)

    """
    # Translate the story to English
    messages.append(
        {'role': 'user',
         'content': f'''
                        Translate this Japanese Story to English. 
                        Make sure to translate all Japanese characters to English.
                    '''
         }
    )
    """

    englishStory = translateToEnglish(japaneseStory)

    result_data = {
        'japaneseStory': japaneseStory,
        'englishStory': englishStory,
        'hiraganaStory': hiraganaStory
    }

    return render_template('englishTranslation.html', result=result_data)

@app.route('/ankiRecord', methods=['POST'])
def ankiRecord():

    selected_words_position = session['selected_words_position']
    selected_words = session['selected_words']

    session['selected_words_position'] = selected_words_position + 1

    if (selected_words_position + 1) < len(selected_words):
        return redirect('anki')
        # return redirect('/App/JapaneseFriendOnline/anki')
    else:
        # Show the English Translation
        return redirect('englishTranslationDynamic')
        # return redirect('/App/JapaneseFriendOnline/englishTranslationDynamic')

@app.route('/japaneseConversation', methods=['POST'])
def japaneseConversation():
    # Save current run datetime in log file
    log_datetime()
    return render_template('japaneseConversation.html')

@app.route('/japaneseScenario', methods=['POST'])
def japaneseScenario():
    result_data = []

    scenarioText = request.form['scenarioText']

    scenarioText = scenarioText + ". You will always respond in simple Japanese that a child can understand."

    # Start a CharGPT conversation with the scenarioText as the system message
    conversationMessages = [
        {'role': 'system',
         'content': scenarioText
         }
    ]

    session['conversationMessages'] = conversationMessages

    return render_template('iSay.html', result=result_data)

@app.route('/iSayDynamic', methods=['POST'])
def iSayDynamic():
    session['iSayText'] = request.form['iSayText']
    iSayText = request.form['iSayText']
    conversationMessages = session['conversationMessages']
    conversationMessages.append(
        {'role': 'user',
         'content': iSayText
         }
    )
    youSayText = get_completion_from_messages(conversationMessages, max_tokens=100)
    session['youSayText'] = youSayText
    conversationMessages.append(
        {'role': 'assistant',
         'content': youSayText
         }
    )
    session['conversationMessages'] = conversationMessages

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
    iSayTextReviewed = withFuriganaHTMLParagraph(correctSpellingGrammar(iSayText))
    return iSayTextReviewed

@app.route('/conversationFuriganaResponse')
def conversationFuriganaResponse():
    youSayText = session['youSayText']
    youSayTextFurigana = withFuriganaHTMLParagraph(youSayText)
    return youSayTextFurigana

@app.route('/iSay', methods=['POST'])
def iSay():
    result_data = []

    iSayText = request.form['iSayText']
    conversationMessages = session['conversationMessages']
    # print(conversationMessages)

    correctedText = correctSpellingGrammar(iSayText)

    conversationMessages.append(
        {'role': 'user',
         'content': correctedText
         }
    )

    youSayText = get_completion_from_messages(conversationMessages, max_tokens=100)
    youSayTextFuriganaHTML = withFuriganaHTMLParagraph(youSayText)

    youSayTextEnglish = translateToEnglish(youSayText)

    iSayTextReviewed = withFuriganaHTMLParagraph(correctedText)

    result_data = {
        'youSayTextFurigana': youSayTextFuriganaHTML,
        'youSayTextEnglish': youSayTextEnglish,
        'iSayTextReviewed': iSayTextReviewed
    }

    conversationMessages.append(
        {'role': 'assistant',
         'content': youSayText
         }
    )
    session['conversationMessages'] = conversationMessages

    return render_template('youSay.html', result=result_data)

@app.route('/youSay', methods=['POST'])
def youSay():
    return render_template('iSay.html')

if __name__ == '__main__':
    app.run(debug=False, port=5001)
