# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# mypy: disable-error-code="arg-type,attr-defined"
# pylint: disable=W0613, W0622

import logging
from typing import Any, Dict, Iterator, List

from app.patterns.custom_rag_qa.templates import (
    inspect_conversation_template,
    rag_template,
    template_docs,
)
from app.utils.output_types import OnChatModelStreamEvent, OnToolEndEvent, custom_chain
import google
from langchain.schema import Document
from langchain.tools import tool
from langchain_core.messages import ToolMessage
from langchain_google_community import VertexAISearchRetriever
from langchain_google_community.vertex_rank import VertexAIRank
from langchain_google_vertexai import ChatVertexAI, VertexAIEmbeddings
import vertexai

# Configuration
EMBEDDING_MODEL = "text-embedding-004"
LLM_MODEL = "gemini-1.5-flash-002"
TOP_K = 5

DATASTORE_ID = "sample_datastore_id_01"
VERTEX_AI_LOCATION = "europe-west2"
AGENT_BUILDER_LOCATION = "eu"

# Initialize logging
logging.basicConfig(level=logging.INFO)

# Initialize Google Cloud and Vertex AI
credentials, project_id = google.auth.default()
vertexai.init(project=project_id)

# Set up embedding model and vertex AI retriever
embedding = VertexAIEmbeddings(
    model_name=EMBEDDING_MODEL, 
    location=VERTEX_AI_LOCATION
)

retriever = VertexAISearchRetriever(
    project_id=project_id,
    data_store_id=DATASTORE_ID,
    location_id=AGENT_BUILDER_LOCATION,
    engine_data_type=1,
    # The ratio is set to 0 by default to not use custom
    # embeddings (We believe the relevance search is more
    # performant but you can adapt the ratio as you need.
    custom_embedding_ratio=0,
    custom_embedding=embedding,
    custom_embedding_field_path="embedding",
    # Extracting 20 documents before re-rank.
    max_documents=20,
)

# Initialize document compressor
compressor = VertexAIRank(
    project_id=project_id,
    location_id="global",
    ranking_config="default_ranking_config",
    title_field="id",
    top_n=TOP_K,
)


@tool
def retrieve_docs(query: str) -> List[Document]:
    """
    Useful for retrieving relevant documents based on a query.
    Use this when you need additional information to answer a question.

    Args:
        query (str): The user's question or search query.

    Returns:
        List[Document]: A list of the top-ranked Document objects, limited to TOP_K (5) results.
    """
    retrieved_docs = retriever.invoke(query)
    ranked_docs = compressor.compress_documents(documents=retrieved_docs, query=query)
    return ranked_docs


@tool
def should_continue() -> None:
    """
    Use this tool if you determine that you have enough context to respond to the questions of the user.
    """
    return None


# Initialize language model
llm = ChatVertexAI(model=LLM_MODEL, temperature=0, max_tokens=1024)

# Set up conversation inspector
inspect_conversation = inspect_conversation_template | llm.bind_tools(
    [retrieve_docs, should_continue], tool_choice="any"
)

# Set up response chain
response_chain = rag_template | llm


@custom_chain
def chain(
    input: Dict[str, Any], **kwargs: Any
) -> Iterator[OnToolEndEvent | OnChatModelStreamEvent]:
    """
    Implement a RAG QA chain with tool calls.

    This function is decorated with `custom_chain` to offer LangChain compatible
    astream_events, support for synchronous invocation through the `invoke` method,
    and OpenTelemetry tracing.
    """
    # Inspect conversation and determine next action
    inspection_result = inspect_conversation.invoke(input)
    tool_call_result = inspection_result.tool_calls[0]

    # Execute the appropriate tool based on the inspection result
    if tool_call_result["name"] == "retrieve_docs":
        # Retrieve relevant documents
        docs = retrieve_docs.invoke(tool_call_result["args"])
        # Format the retrieved documents
        formatted_docs = template_docs.format(docs=docs)
        # Create a ToolMessage with the formatted documents
        tool_message = ToolMessage(
            tool_call_id=tool_call_result["name"],
            name=tool_call_result["name"],
            content=formatted_docs,
            artifact=docs,
        )
    else:
        # If no documents need to be retrieved, continue with the conversation
        tool_message = should_continue.invoke(tool_call_result)

    # Update input messages with new information
    input["messages"] = input["messages"] + [inspection_result, tool_message]

    # Yield tool results metadata
    yield OnToolEndEvent(
        data={"input": tool_call_result["args"], "output": tool_message}
    )

    # Stream LLM response
    for chunk in response_chain.stream(input=input):
        yield OnChatModelStreamEvent(data={"chunk": chunk})