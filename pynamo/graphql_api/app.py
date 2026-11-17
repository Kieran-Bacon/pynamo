import os

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import AppSyncResolver
from aws_lambda_powertools.utilities.typing import LambdaContext

from .resolvers.assessment_invitation_handler import (
    router as assessment_invitation_router,
)
from .resolvers.assessment_log_handler import router as assessment_log_router
from .resolvers.award_handler import router as award_router
from .resolvers.device_management_handler import router as device_management_router
from .resolvers.feedback_handler import router as feedback_router
from .resolvers.goal_handler import router as goal_router
from .resolvers.goal_metric_handler import router as goal_metric_router
from .resolvers.leaderboard_handler import router as leaderboard_router
from .resolvers.measure_handler import router as measure_router
from .resolvers.metric_handler import router as metric_router
from .resolvers.milestone_handler import router as milestone_router
from .resolvers.next_action_handler import router as next_action_handler
from .resolvers.programme_handler import router as programme_router
from .resolvers.prompt_handler import router as prompt_router
from .resolvers.recommender_handler import router as recommender_router
from .resolvers.user_handler import router as user_router
from .resolvers.workout_handler import router as workout_router
from .router.connectors import AppSyncConnector
from .router.exceptions import GraphQLQueryValidationError
from .router.graphql_router import GraphQLRouter

logger = Logger(service="GQL Resolver")
app = AppSyncResolver()

# Required environment variables
dynamo_table_name = os.environ["DYNAMO_TABLE"]
user_pool_id = os.environ["USER_POOL_ID"]
logger.info(
    f"Initializing with environment variables:\nDynamo table: {dynamo_table_name}\nUser Pool: {user_pool_id}"
)


# app.include_router(user_router)
app.include_router(prompt_router)
app.include_router(recommender_router)
app.include_router(programme_router)
app.include_router(metric_router)
# app.include_router(goal_router)
# app.include_router(workout_log_router)
app.include_router(leaderboard_router)
# app.include_router(feedback_router)
# app.include_router(award_router)
# app.include_router(milestone_router)

dynamodb = boto3.resource("dynamodb")
dynamo_table = dynamodb.Table(dynamo_table_name)
# dynamo_table.key_schema # Trigger the table object make a call making the first use in a request faster
cognito_client = boto3.client("cognito-idp")

custom_router = GraphQLRouter.combine(
    [
        user_router,
        next_action_handler,
        workout_router,
        award_router,
        milestone_router,
        feedback_router,
        goal_router,
        goal_metric_router,
        measure_router,
        assessment_invitation_router,
        assessment_log_router,
        device_management_router,
    ]
)
appsync_connector = AppSyncConnector(custom_router)

custom_router.context = {
    "dynamo_table": dynamo_table,
    "user_pool_id": user_pool_id,
    "cognito_client": cognito_client,
}
