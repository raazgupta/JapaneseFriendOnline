'''
# Import the activate_this.py script from the virtual environment
activate_this = '/home/public/App/JapaneseFriendOnline/venv/bin/activate_this.py'
with open(activate_this) as file_:
    exec(file_.read(), dict(__file__=activate_this))
'''

from flask import Flask, render_template, session, redirect, url_for, request
import openai
import os
import random
from datetime import datetime
import requests

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SESSION_SECRET_KEY')


def get_completion_from_messages(messages, model="gpt-3.5-turbo", temperature=0.0, max_tokens=500):
    openai.api_key = os.environ.get('OPENAI_API_KEY')
    # print("Get completion message: ", messages)
    response = openai.ChatCompletion.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message["content"]

def get_response_from_wanikani(url_end = ""):
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
        print("JSON response:")
        print(data)
        return data
    else:
        print(f"Wanikani API Error: {response.status_code}")
        return None


# Get assignments where levels=(1 to current level) and immediately available for review and subject_types=vocubulary
# This is a recursive function that goes up to Level 60, which is the max user level
def addVocabsInAscendingOrder(user_level, current_level_position, vocab_ids):
    if user_level > current_level_position:
        levels_string = ""
        for counter in range(1,11):
            current_level_position = current_level_position + 1
            if counter != 10:
                levels_string = levels_string + str(current_level_position) + ","
            else:
                levels_string = levels_string + str(current_level_position)
        url_end = f"assignments?levels={levels_string}&subject_types=vocabulary&immediately_available_for_review"
        assignments_json_dict = get_response_from_wanikani(url_end=url_end)
        if assignments_json_dict != None:
            current_vocab_ids = [item['data']['subject_id'] for item in assignments_json_dict['data']]
            number_missing = 10 - len(vocab_ids)
            if len(current_vocab_ids) > number_missing:
                current_vocab_ids = random.sample(current_vocab_ids, number_missing)
                vocab_ids.extend(current_vocab_ids)
                return vocab_ids
            else:
                vocab_ids.extend(current_vocab_ids)
                return addVocabsInAscendingOrder(user_level, current_level_position, vocab_ids)
    else:
        return vocab_ids

def chooseSelectedWords(subject_types="vocabulary"):
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
                # From assignment, randomly select 10 kanji
                # Extract 'subject_id' values from the 'data' list
                kanji_ids = [item['data']['subject_id'] for item in assignments_json_dict['data']]
                # print(kanji_ids)
                if len(kanji_ids) > 10:
                    kanji_ids = random.sample(kanji_ids, 10)
                    # print(kanji_ids)
                for kanji_id in kanji_ids:
                    # Get the subject details
                    url_end = "subjects/" + str(kanji_id)
                    kanji_subject_json_dict = get_response_from_wanikani(url_end=url_end)
                    if kanji_subject_json_dict != None:
                        # For each kanji, randomly select 1 amalgamation_subject_id (vocabulary)
                        vocabs_list = kanji_subject_json_dict['data']['amalgamation_subject_ids']
                        random_vocab = random.sample(vocabs_list, 1)
                        # print(random_vocab)
                        # Get [Word, Hiragana, Meaning] for each vocab and append to selected_words
                        url_end = "subjects/" + str(random_vocab[0])
                        vocab_subject_json_dict = get_response_from_wanikani(url_end=url_end)
                        if vocab_subject_json_dict != None:
                            word = vocab_subject_json_dict['data']['characters']
                            hiragana = vocab_subject_json_dict['data']['readings'][0]['reading']
                            meaning = vocab_subject_json_dict['data']['meanings'][0]['meaning']
                            selected_words.append([word, hiragana, meaning])
        elif subject_types == "vocabulary":
            # Get assignments where levels=(1 to current level) and immediately available for review and subject_types=vocubulary

            vocab_ids = addVocabsInAscendingOrder(user_level, 0, [])

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


def create_story(selected_words, temperature=0.0):
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
            Write a story in Japanese with maximum 3 sentences.
            The story should be written in simple Japanese that a child can understand. 
            Make sure these words are in the story: {",".join(words)}.
         """
         },
    ]

    # Print story in German
    response = get_completion_from_messages(messages, temperature=temperature)
    print("Japanese Story:")
    print(response)

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

    print(request.form.get('category'))
    selected_words = chooseSelectedWords(request.form.get('category'))
    print(selected_words)
    temperature = (10 - len(selected_words))/10
    messages, response = create_story(selected_words, temperature=temperature)

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
    print(result_data)

    return render_template('ankiTranslate.html', result=result_data)

@app.route('/englishTranslation', methods=['POST','GET'])
def englishTranslation():
    messages = session['messages']
    japaneseStory = session['japaneseStory']

    # Get the response in All Hiragana
    messages.append(
        {'role': 'user',
         'content': f'Convert all Kanji characters to Hiragana characters in this Japanese Story.'
         }
    )
    hiraganaStory = get_completion_from_messages(messages)

    # Translate the story to English
    messages.append(
        {'role': 'user',
         'content': f"""
                        Translate this Japanese Story to English. 
                        Make sure to translate all Japanese characters to english including the vocabulary within square brackets.
                    """
         }
    )
    englishStory = get_completion_from_messages(messages)

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
        return redirect(url_for('anki', _external=False))
        # return redirect('/App/JapaneseFriendOnline/anki')
    else:
        # Show the English Translation
        return redirect(url_for('englishTranslation', _external=False))
        # return redirect('/App/JapaneseFriendOnline/englishTranslation')

if __name__ == '__main__':
    app.run(debug=False)