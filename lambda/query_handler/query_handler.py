"""
RAG query handler: retrieves relevant chunks from the Bedrock Knowledge Base,
then calls Claude Haiku to generate a grounded answer.
"""
import os
import json
import boto3

KB_ID = os.environ["KB_ID"]
REGION = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "us-east-1"))
MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
NUM_RESULTS = 6

bedrock_agent = boto3.client("bedrock-agent-runtime", region_name=REGION)
bedrock = boto3.client("bedrock-runtime", region_name=REGION)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
}

SYSTEM_PROMPT = """You are an expert Elden Ring guide. Answer the player's question
using ONLY the information in the provided context. Be specific: include locations,
nearby landmarks, and item/enemy names when available. If the context does not
contain enough information to answer, say so clearly rather than guessing.
Do not include markdown formatting in your answer and do not use the term 'context' in your answer. State which source it is from if relevant.
Always use all available information from the retrieved chunks to provide the best answer possible.
Try to keep your answer concise, conversational, and to the point, while still being as informative as possible.
Avoid long lists if possible - instead, try to synthesize the information into a coherent answer.
Make your best guess at terms that might be misspelled in the question, if you're unsure, ask with brevity to repeat the term.
"""


def handler(event, context):
    # Handle CORS preflight
    if event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}

    try:
        body = json.loads(event.get("body") or "{}")
        question = (body.get("question") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        return _err(400, "Request body must be JSON with a 'question' field.")

    if not question:
        return _err(400, "'question' field is required.")
    if len(question) > 1000:
        return _err(400, "Question must be 1000 characters or fewer.")

    # 1. Retrieve relevant chunks from the Knowledge Base
    try:
        retrieval = bedrock_agent.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": question},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": NUM_RESULTS}
            },
        )
    except Exception as e:
        print(f"Retrieval error: {e}")
        return _err(502, "Failed to retrieve context from knowledge base.")

    results = retrieval.get("retrievalResults", [])
    if not results:
        return _ok({"answer": "I couldn't find relevant information for that question. "
                              "The knowledge base may still be syncing or the topic may not be covered."})

    context_text = "\n\n---\n\n".join(
        r["content"]["text"] for r in results if r.get("content", {}).get("text")
    )

    # 2. Generate answer with Claude Haiku
    prompt = f"Context:\n{context_text}\n\nQuestion: {question}"
    try:
        response = bedrock.invoke_model(
            modelId=MODEL_ID,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 600,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        result = json.loads(response["body"].read())
        answer = result["content"][0]["text"]
    except Exception as e:
        print(f"Inference error: {e}")
        return _err(502, "Failed to generate answer from the model.")

    # 3. Return answer + source snippets for transparency
    sources = [
        {
            "excerpt": r["content"]["text"][:200],
            "score": r.get("score"),
            "location": r.get("location", {}).get("s3Location", {}).get("uri", ""),
        }
        for r in results
    ]

    return _ok({"answer": answer, "sources": sources})


def _ok(body: dict) -> dict:
    return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps(body)}


def _err(status: int, message: str) -> dict:
    return {"statusCode": status, "headers": CORS_HEADERS,
            "body": json.dumps({"error": message})}
