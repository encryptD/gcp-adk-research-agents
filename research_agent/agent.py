from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import agent_tool
from google.adk.tools import url_context
from google.adk.tools.google_search_tool import GoogleSearchTool
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.genai import types

from research_agent.prompts import load_prompt
from research_agent.rate_limit import RateLimitPlugin

# Shared model with HTTP retries for transient 429/5xx responses.
MODEL = Gemini(
    model="gemini-3.6-flash",
    retry_options=types.HttpRetryOptions(
        attempts=3,
        initial_delay=1.0,
        max_delay=8.0,
        exp_base=2.0,
        http_status_codes=[429, 500, 503, 504],
    ),
)

RESEARCH_PACKET_MCP_URL = (
    "https://research-packet-mcp-444751192396.us-central1.run.app/mcp"
)
GCP_RESEARCH_GATEWAY_MCP_URL = (
    "https://gcp-research-gateway-444751192396.us-central1.run.app/mcp"
)


def research_packet_mcp() -> McpToolset:
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=RESEARCH_PACKET_MCP_URL),
    )


def gcp_research_gateway_mcp() -> McpToolset:
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=GCP_RESEARCH_GATEWAY_MCP_URL
        ),
    )


research_agent_google_search_agent = LlmAgent(
    name="Research_Agent_google_search_agent",
    model=MODEL,
    description="Agent specialized in performing Google searches.",
    sub_agents=[],
    instruction=load_prompt("google_search_agent"),
    tools=[GoogleSearchTool()],
)

research_agent_url_context_agent = LlmAgent(
    name="Research_Agent_url_context_agent",
    model=MODEL,
    description="Agent specialized in fetching content from URLs.",
    sub_agents=[],
    instruction=load_prompt("research_url_context_agent"),
    tools=[url_context],
)

research_agent = LlmAgent(
    name="research_agent",
    model=MODEL,
    description="This is Research subagent",
    sub_agents=[],
    instruction=load_prompt("research_agent"),
    tools=[
        agent_tool.AgentTool(agent=research_agent_google_search_agent),
        agent_tool.AgentTool(agent=research_agent_url_context_agent),
        research_packet_mcp(),
        gcp_research_gateway_mcp(),
    ],
)

reviewer_agent_url_context_agent = LlmAgent(
    name="Reviewer_Agent_url_context_agent",
    model=MODEL,
    description="Agent specialized in fetching content from URLs.",
    sub_agents=[],
    instruction=load_prompt("reviewer_url_context_agent"),
    tools=[url_context],
)

reviewer_agent = LlmAgent(
    name="reviewer_agent",
    model=MODEL,
    description="This Is Reviewer Subagent",
    sub_agents=[],
    instruction=load_prompt("reviewer_agent"),
    tools=[
        agent_tool.AgentTool(agent=reviewer_agent_url_context_agent),
        research_packet_mcp(),
        gcp_research_gateway_mcp(),
    ],
)

synthesis_agent = LlmAgent(
    name="synthesis_agent",
    model=MODEL,
    description="This Is Synthesis SubAgent",
    sub_agents=[],
    instruction=load_prompt("synthesis_agent"),
    tools=[research_packet_mcp()],
)

root_agent = LlmAgent(
    name="Router",
    model=MODEL,
    description="This is Router Agent",
    sub_agents=[research_agent, reviewer_agent, synthesis_agent],
    instruction=load_prompt("router_agent"),
    tools=[gcp_research_gateway_mcp()],
)

app = App(
    root_agent=root_agent,
    name="research_agent",
    plugins=[
        RateLimitPlugin(
            max_auto_retries=1,
            initial_backoff_seconds=1.0,
            backoff_multiplier=2.0,
        )
    ],
)
