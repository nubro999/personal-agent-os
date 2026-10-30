#!/bin/bash
# 2325 DB(generativelab-client-db-2) 전용 SSM 터널. maintenance_list/run.sh 없이 수집기가 읽을 수 있게 한다.
# run.sh(13306)와 겹치지 않게 13316을 쓴다. 대상 ECS 태스크는 배포마다 바뀌어서 시작할 때마다 조회한다.
set -euo pipefail

PROFILE=${AWS_PROFILE:-genlab}
REGION=ap-northeast-2
CLUSTER=genlab-module-prod
SERVICE=rds-tunnel
DB_HOST=generativelab-client-db-2.cygg3sfzdxtg.ap-northeast-2.rds.amazonaws.com
LOCAL_PORT=${PAO_MAINT_DB_PORT:-13316}

task=$(aws ecs list-tasks --profile "$PROFILE" --region "$REGION" \
  --cluster "$CLUSTER" --service-name "$SERVICE" --query 'taskArns[0]' --output text)
runtime=$(aws ecs describe-tasks --profile "$PROFILE" --region "$REGION" \
  --cluster "$CLUSTER" --tasks "$task" --query 'tasks[0].containers[0].runtimeId' --output text)

exec aws ssm start-session --profile "$PROFILE" --region "$REGION" \
  --target "ecs:${CLUSTER}_${task##*/}_${runtime}" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$DB_HOST\"],\"portNumber\":[\"3306\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}"
