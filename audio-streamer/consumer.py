import os
from google.cloud import pubsub_v1

project_id = "sandbox-427708"
pub_sub_topic = "live-translation"
pub_sub_subscription = "live-translation-sub"

location = "global"

topic_name = 'projects/{project_id}/topics/{topic}'.format(
    project_id=project_id,
    topic=pub_sub_topic,
)

subscription_name = 'projects/{project_id}/subscriptions/{sub}'.format(
    project_id=project_id,
    sub=pub_sub_subscription,
)

def callback(message):
    print(message.data.decode("utf-8"))
    message.ack()

with pubsub_v1.SubscriberClient() as subscriber:
    # subscriber.create_subscription(name=subscription_name, topic=topic_name)
    
    future = subscriber.subscribe(subscription_name, callback)
    
    try:
        future.result()
    except KeyboardInterrupt:
        future.cancel()