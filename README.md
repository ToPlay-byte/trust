
# How to start project

## 1. Create or enter in env

```bash
python3 -m venv env
source env/bin/activate
```

## 2. Install dependencies

```bash
pip3 install -r requirements.txt
```

## 3. Edit trustpilot/modules/`load_django.py` and set the project path
```python
sys.path.append(' ___ /trustpilot-warming_strategy/trustpilot') 
```

## 4. Prepare `.env` file with DB credentials

```bash
DB_NAME = 'trustpilot'
DB_USER = ''
DB_PASSWORD = ''
DB_HOST = 'localhost'
DB_PORT = '5001'

PGADMIN_DEFAULT_EMAIL = 'admin@mail.com'
PGADMIN_DEFAULT_PASSWORD = 'admin'

DEBUG = True
DEBUG_DELAY = 1000

OPENAI_API_KEY = ''
```

## 5. Run docker compose

```bash
docker compose up
```

## 6. Migrations

```bash
python trustpilot/manage.py makemigrations
python trustpilot/manage.py migrate
```
<br>
<br>
<br>

# How to work with project

## Task management works via DB. There are 4 tables:
- `user`: defines which account the task belongs to and stores general account information and status
- `profile`: defines the profile information in the account, such as profile ID and folder ID from Multilogin, and is linked to the `user` table 
- `usertaskmanager`: defines the tasks assigned to profiles, including status, parameters, and results
- `usertasklog`: stores logs related to profile tasks, including messages and log levels

### Required parameters
- `action`: task name, for example `day_1_2` or `day_3_4`
- `task_status`: current task status, for example `pending`, `in_progress`, `success`, or `failed`
- `execute_at`: timestamp when the task should start, example - `2026-04-25 09:00:00+00`
- `profile_id`: Profile ID from profile user table

### Other task fields
- `started_at`: timestamp when the task started
- `finished_at`: timestamp when the task was completed
- `duration`: time taken to complete the task
- `comment`: additional information or notes about the task
- `updated_at`: timestamp when the task was last updated

## Task parameters (optional, depending on the task):
- `company_queries`: list of company queries
- `interactions_count_from`: minimum number of interactions
- `interactions_count_to`: maximum number of interactions
- `company_queries_count_from`: minimum number of company queries
- `company_queries_count_to`: maximum number of company queries
- `limit_on_reviews_duration`: calculate average time spent per page reviews and check if enough time remains to read long reviews, if not - go next page
- `pause_multiplier`: multiplier for task pause duration
- `prompt_parameters`: by default it's - "neutral, slightly positive, realistic tone, preferably for a 4 start out of 5 rating" - added to the final prompt as "Additional parameters: ..." to adjust the style of the review to be generated
- `prompts`: list of prompts, for example - `["Write a review based on the following company information and reviews: ..."]` - if not provided, default prompt will be used
- `restricted_company_queries`: list of restricted company queries, which will be omitted from the task execution if they are present
- `review_length`: length of the review in words to be generated, integer value 
- `target_company`: target company for the task
- `profile_id`: Profile ID from profile user table
- `maximum_longer_reviews`: maximum number of longer reviews to read
- `maximum_review_pages`: maximum number of review pages to read

### Task result 
- `interactions_count`: number of interactions completed during task execution
- `reviews_count`: number of reviews posted during task execution

```
The minimum required parameters for a task are `action`, `task_status`, `execute_at`, `profile_id`. Other parameters can be added based on the specific requirements of the task being created.
```