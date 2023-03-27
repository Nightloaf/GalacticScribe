import os
import sys
import re
import praw
import configparser
import smtplib
import schedule
import time
import ssl
import logging
import json
from pathlib import Path
from datetime import datetime
from bs4 import BeautifulSoup
from ebooklib import epub
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from tenacity import retry, stop_after_attempt, wait_exponential

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Read configuration
config = configparser.ConfigParser()
config.read('config.ini')

# Authenticate with Reddit
reddit = praw.Reddit(
    client_id=config['REDDIT']['client_id'],
    client_secret=config['REDDIT']['client_secret'],
    user_agent=config['REDDIT']['user_agent'],
    username=config['REDDIT']['username'],
    password=config['REDDIT']['password']
)

# Get the authors and their stories from the configuration
authors_stories = {}
for author, stories in config['STORIES'].items():
    authors_stories[author] = [s.strip() for s in stories.split(',')]

subreddit_name = 'hfy'

def sanitize_title(title):
    return re.sub(r'[<>:"/\\|?*]', '_', title)

def validate_story(chapter_title, html_content):
    # Check for a minimum length
    if len(html_content) < 100:
        return False, f"Chapter '{chapter_title}' is too short."

    # Check for missing opening or closing tags
    soup = BeautifulSoup(html_content, 'html.parser')
    if soup.find_all(lambda tag: tag.name in ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'] and not tag.text.strip()):
        return False, f"Chapter '{chapter_title}' contains empty tags."

    # You can add more validation checks as needed
    return True, ""

def get_chapters(author, story):
    redditor = reddit.redditor(author)
    submissions = redditor.submissions.new(limit=None)
    return [submission for submission in submissions if submission.subreddit.display_name.lower() == subreddit_name.lower() and story.lower() in submission.title.lower()]

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=60))
def download_stories():
    summary = []
    failed_files = []
    if config['SETTINGS'].getboolean('bot_enabled'):
        for author, stories in authors_stories.items():
            for story in stories:
                story_dir = os.path.join(os.getcwd(), sanitize_title(story))
                Path(story_dir).mkdir(parents=True, exist_ok=True)

                epub_book = epub.EpubBook()
                epub_book.set_title(story)
                epub_book.set_language('en')
                epub_book.add_author(author)

                spine = ['nav']
                toc = []

                chapter_submissions = {}

                # Collect all the chapter submissions in a dictionary
                for submission in get_chapters(author, story):
                    # Replace older submission with newer one if titles are the same
                    if submission.title in chapter_submissions:
                        if submission.created_utc > chapter_submissions[submission.title].created_utc:
                            chapter_submissions[submission.title] = submission
                    else:
                        chapter_submissions[submission.title] = submission

                # Sort the submissions by submission date (ascending)
                sorted_submissions = sorted(chapter_submissions.values(), key=lambda x: x.created_utc)

                # Process the chapters in the sorted order
                for submission in sorted_submissions:
                    chapter_title = submission.title
                    sanitized_chapter_title = sanitize_title(chapter_title)
                    html_content = submission.selftext_html

                    is_valid, validation_message = validate_story(chapter_title, html_content)
                    if not is_valid:
                        logging.error(validation_message)
                        continue
                    soup = BeautifulSoup(html_content, 'html.parser')

                    chapter = epub.EpubHtml(title=chapter_title, file_name=f'{sanitized_chapter_title}.xhtml')
                    chapter.content = str(soup)
                    epub_book.add_item(chapter)

                    toc.append(chapter)
                    spine.append(chapter)

                epub_book.toc = toc
                epub_book.spine = spine

                epub_path = os.path.join(story_dir, f'{sanitize_title(story)}.epub')
                epub.write_epub(epub_path, epub_book)

                # Send the EPUB file via email and delete it
                try:
                    send_email(epub_path, story, config['EMAIL']['receiver'])
                    os.remove(epub_path)
                    summary.append(f"Successfully processed and sent {story} by {author}")
                    log_email_sent(story)
                except Exception as e:
                    error_message = f"Failed to send and delete EPUB for {story}: {e}"
                    logging.error(error_message)
                    send_email(None, f"Error - {story}", config['EMAIL']['error_receiver'], error_message)
                    summary.append(error_message)
                    failed_files.append(epub_path)

    summary_email_body = "Summary:\n\n" + "\n".join(summary)
    send_email(None, "Stories Download Summary", config['EMAIL']['error_receiver'], summary_email_body)



def log_email_sent(story_title):
    log_dir = 'email_logs'
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    log_date = time.strftime('%Y-%m-%d')
    log_file_path = os.path.join(log_dir, f'{log_date}_email_sent.log')

    with open(log_file_path, 'a') as log_file:
        log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {story_title}\n")

    clean_old_logs(log_dir, max_size_mb=25)

def clean_old_logs(log_dir, max_size_mb):
    log_files = sorted(Path(log_dir).glob('*.log'), key=os.path.getctime)
    total_size_mb = sum(os.path.getsize(f) for f in log_files) / (1024 * 1024)

    while total_size_mb > max_size_mb:
        oldest_log = log_files.pop(0)
        os.remove(oldest_log)
        total_size_mb = sum(os.path.getsize(f) for f in log_files) / (1024 * 1024)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=60))
def send_email(file_path, story_title, receiver_email, error_message=None):
    message = MIMEMultipart()
    message['From'] = config['EMAIL']['sender']
    message['To'] = receiver_email
    message['Subject'] = story_title if not error_message else f'Error: {story_title}'

    if file_path:
        with open(file_path, 'rb') as attachment:
            attach = MIMEBase('application', 'octet-stream')
            attach.set_payload(attachment.read())

        encoders.encode_base64(attach)
        attach.add_header('Content-Disposition', f'attachment; filename="{Path(file_path).name}"')
        message.attach(attach)

    if error_message:
        message.attach(MIMEText(error_message, 'plain'))

    context = ssl.create_default_context()
    with smtplib.SMTP(config['EMAIL']['smtp_server'], int(config['EMAIL']['smtp_port'])) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(config['EMAIL']['username'], config['EMAIL']['password'])
        server.sendmail(config['EMAIL']['sender'], receiver_email, message.as_string())

def job():
    try:
        download_stories()
    except Exception as e:
        logging.error(f"Failed to download stories: {e}")
        send_email(None, "Bot Error", config['EMAIL']['error_receiver'], f"Bot encountered an error: {e}")

# Schedule the bot to run hourly
#schedule.every(1).hour.do(job)

# Send start notification
send_email(None, "Bot Started", config['EMAIL']['error_receiver'], "The bot has started.")

job()

#while True:
#    try:
#        schedule.run_pending()
#        time.sleep(1)
#    except Exception as e:
#        logging.error(f"Bot encountered an error: {e}")
#        send_email(None, "Bot Error", config['EMAIL']['error_receiver'], f"Bot encountered an error: {e}")
