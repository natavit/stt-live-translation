# Live Speech-to-Text Translation

- Create a Google Pub/Sub topic with a name `live-translation` or any name you want.

```
export PROJECT_ID=$(gcloud config get-value project)
export LOCATION="LOCATION"
export PUBSUB_TOPIC="TOPIC_NAME"
export REPOSITORY="REPOSITORY"
export IMAGE="IMAGE"
export TAG="TAG"

gcloud builds submit --tag $LOCATION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/$IMAGE:$TAG

export SERVICE_NAME="SERVICE_NAME"

gcloud run deploy $SERVICE_NAME \
--image $LOCATION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/$IMAGE:$TAG \
--platform managed \
--region $LOCATION \
--set-env-vars PROJECT_ID=$PROJECT_ID \
--allow-unauthenticated
```