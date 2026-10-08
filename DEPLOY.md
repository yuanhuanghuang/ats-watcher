# ATS Watcher — Deployment (Phase 4)

Run these yourself; no AWS credentials ever go into the code. Assumes the AWS
CLI is configured (`aws configure`) with the same account as your daily job
agent. Adjust `REGION`, bucket name, etc.

## 0. Prereqs / variables

```bash
export REGION=us-east-1
export FUNCTION_NAME=ats-watcher
export ROLE_NAME=ats-watcher-lambda-role
export STATE_BUCKET=ats-watcher-state-$(aws sts get-caller-identity --query Account --output text)
export STATE_KEY=ats-watcher/state.json
```

## 1. Confirm the watchlist is current

```bash
# Provide your own seed CSV (columns: company,slug_hint,tier,category,notes).
python3 discovery/discover_slugs.py --seed companies_seed.csv   # resolves slugs -> watchlist.json
python3 -c "import json;print(len(json.load(open('watchlist.json'))),'boards')"
```

## 2. Package the deployment zip

`watchlist.json` is bundled in the zip (the code reads it locally); only
`state.json` lives in S3.

Email uses Gmail SMTP (stdlib), so the only third-party deps to bundle are
aiohttp (+ C-ext deps) and certifi. They must be **Linux x86_64** wheels, not
your local macOS ones:

```bash
rm -rf package watcher.zip
python3 -m pip install aiohttp certifi typing_extensions \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 \
  --only-binary=:all: --target package/
(cd package && zip -rq ../watcher.zip . -x '*/__pycache__/*' -x 'bin/*')
zip -q watcher.zip watcher/*.py watchlist.json
```

## 3. S3 bucket for state

```bash
aws s3 mb s3://$STATE_BUCKET --region $REGION
```

## 4. IAM role (Lambda basic execution + S3 to the state bucket)

```bash
# Trust policy
cat > /tmp/trust.json <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON

aws iam create-role --role-name $ROLE_NAME \
  --assume-role-policy-document file:///tmp/trust.json

aws iam attach-role-policy --role-name $ROLE_NAME \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# S3 on the state bucket only. ListBucket is required as well as Get/Put:
# without it S3 answers AccessDenied instead of NoSuchKey for a missing key,
# and storage.load() only treats NoSuchKey as a first run — so a fresh bucket
# would fail every invocation instead of seeding. Note the two resource ARNs:
# object actions take /*, ListBucket takes the bare bucket.
cat > /tmp/s3.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],
  "Resource":"arn:aws:s3:::$STATE_BUCKET/*"},
 {"Effect":"Allow","Action":["s3:ListBucket"],
  "Resource":"arn:aws:s3:::$STATE_BUCKET"}]}
JSON

aws iam put-role-policy --role-name $ROLE_NAME \
  --policy-name ats-watcher-s3 --policy-document file:///tmp/s3.json

export ROLE_ARN=$(aws iam get-role --role-name $ROLE_NAME --query 'Role.Arn' --output text)
sleep 10   # let the role propagate
```

## 5. Create the function

```bash
aws lambda create-function \
  --function-name $FUNCTION_NAME \
  --runtime python3.12 \
  --handler watcher.lambda_function.handler \
  --role $ROLE_ARN \
  --timeout 300 --memory-size 512 \
  --zip-file fileb://watcher.zip \
  --region $REGION \
  --environment "Variables={GMAIL_SENDER=$GMAIL_SENDER,GMAIL_APP_PASSWORD=$GMAIL_APP_PASSWORD,EMAIL_RECIPIENTS=$EMAIL_RECIPIENTS,STATE_BUCKET=$STATE_BUCKET,STATE_KEY=$STATE_KEY}"
```

To update code later:
```bash
aws lambda update-function-code --function-name $FUNCTION_NAME \
  --zip-file fileb://watcher.zip --region $REGION
```

## 6. First (seed) invocation — no email is sent on the first run

```bash
aws lambda invoke --function-name $FUNCTION_NAME --region $REGION /tmp/out.json
cat /tmp/out.json   # expect "first_run": true
```

## 7. Hourly EventBridge trigger

```bash
aws events put-rule --name ats-watcher-hourly \
  --schedule-expression 'rate(1 hour)' --region $REGION

aws lambda add-permission --function-name $FUNCTION_NAME \
  --statement-id ats-watcher-eventbridge --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn $(aws events describe-rule --name ats-watcher-hourly --region $REGION --query Arn --output text) \
  --region $REGION

aws events put-targets --rule ats-watcher-hourly --region $REGION \
  --targets "Id=1,Arn=$(aws lambda get-function --function-name $FUNCTION_NAME --region $REGION --query 'Configuration.FunctionArn' --output text)"
```

Done. From the second run on, new matching jobs arrive by email within the hour.
The Sunday run also emails a weekly health report — once per ISO week, gated on
`last_weekly_report` in state, because this schedule fires hourly.

## 8. Day-to-day operation

Use `./trigger.sh` rather than hand-rolling `aws lambda invoke` (see README):

```bash
./trigger.sh status            # config + schedule + state summary
./trigger.sh preview A,B       # what a backlog sweep would send, mails nothing
./trigger.sh backlog A         # mail Tier A's currently-open backlog
./trigger.sh run               # force one diff cycle
./trigger.sh logs 120          # last 120 min of INFO/WARN/ERROR
```

The seed run in step 6 deliberately mails nothing, so every role already open at
that moment is marked seen and never sent. `backlog` is how you surface it; it
touches no state and is safe to re-run.

## Adding companies later (Phase 5)

```bash
# 1. append rows to your seed CSV
# 2. incremental discovery (checkpoint skips already-resolved companies)
python3 discovery/discover_slugs.py --seed companies_seed.csv
# 3. repackage + update code (step 2 + step 5-update above)
```

## Local dev

```bash
pip install -r requirements.txt
python -m watcher.lambda_function --local --once   # local state file, prints instead of emailing
```
