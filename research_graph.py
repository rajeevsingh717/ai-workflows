"""LangGraph deep-research workflow."""
import os
import json
from operator import add
from typing import TypedDict, Annotated, Literal

from langgraph.graph import StateGraph, START, END
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from search_tools import tavily_search, reddit_search


class ResearchState(TypedDict):
    topic: str
    sub_questions: list[str]
    web_results: Annotated[list[dict], add]
    reddit_results: Annotated[list[dict], add]
    notes: Annotated[list[str], add]
    iterations: int
    max_iterations: int
    reflection: str
    final_report: str


def _llm():
    return ChatAnthropic(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        temperature=0.2,
        max_tokens=4096,
    )


def _parse_json(text: str, default):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("` \n")
    try:
        return json.loads(text)
    except Exception:
        return default


PLAN_PROMPT = """You are a research strategist. Given a topic (and any prior findings),
produce 3-5 focused sub-questions to investigate next. Sub-questions should be
specific, search-friendly, and complementary (not redundant).

Topic: {topic}

Prior findings (most recent first):
{prior}

Gaps identified by the critic (focus here if non-empty):
{reflection}

Return ONLY a JSON array of strings. No prose, no markdown."""


def plan_node(state: ResearchState) -> dict:
    prior = "\n".join(state.get("notes", [])[-3:]) or "(no prior findings yet)"
    reflection = state.get("reflection") or "(none)"
    msg = _llm().invoke([
        SystemMessage(content="You output only valid JSON arrays of strings."),
        HumanMessage(content=PLAN_PROMPT.format(
            topic=state["topic"], prior=prior, reflection=reflection,
        )),
    ])
    questions = _parse_json(msg.content, default=[state["topic"]])
    if not isinstance(questions, list) or not questions:
        questions = [state["topic"]]
    print(f"  ↳ sub-questions: {questions}")
    return {"sub_questions": questions}


def search_web_node(state: ResearchState) -> dict:
    results = []
    for q in state["sub_questions"]:
        print(f"  ↳ web: {q}")
        for r in tavily_search(q, max_results=4):
            results.append({"question": q, **r})
    print(f"  ↳ web results: {len(results)}")
    return {"web_results": results}


def search_reddit_node(state: ResearchState) -> dict:
    results = []
    queries = [state["topic"]] + state["sub_questions"][:2]
    for q in queries:
        print(f"  ↳ reddit: {q}")
        for r in reddit_search(q, limit=4):
            results.append({"question": q, **r})
    print(f"  ↳ reddit results: {len(results)}")
    return {"reddit_results": results}


ANALYZE_PROMPT = """You are a research analyst. Synthesize the sources below into
8-12 concise bullet findings about the topic. Each bullet must cite source numbers
inline like [1], [3]. Prefer specifics (numbers, names, dates) over generalities.
Note disagreements between sources where present.

Topic: {topic}

Sources:
{sources}

Findings (bulleted):"""


def _format_sources(state: ResearchState) -> str:
    lines = []
    idx = 1
    for r in state["web_results"][-24:]:
        lines.append(
            f"[{idx}] WEB · {r.get('title','')} — {r.get('url','')}\n"
            f"{(r.get('content') or '')[:900]}"
        )
        idx += 1
    for r in state["reddit_results"][-16:]:
        lines.append(
            f"[{idx}] REDDIT r/{r.get('subreddit','')} · {r.get('title','')} "
            f"(score {r.get('score',0)}) — {r.get('url','')}\n"
            f"{(r.get('content') or '')[:900]}"
        )
        idx += 1
    return "\n\n".join(lines) if lines else "(no sources)"


def analyze_node(state: ResearchState) -> dict:
    sources = _format_sources(state)
    msg = _llm().invoke([
        HumanMessage(content=ANALYZE_PROMPT.format(topic=state["topic"], sources=sources)),
    ])
    return {"notes": [msg.content]}


REFLECT_PROMPT = """You are a research critic. Decide whether the current findings on
the topic are sufficient for a thorough report, or what gaps remain.

Topic: {topic}

Findings so far:
{notes}

Iteration: {iteration} / {max_iter}

Reply with strict JSON:
{{"done": true|false, "gaps": "describe what still needs investigation, or empty string"}}"""


def reflect_node(state: ResearchState) -> dict:
    notes = "\n\n---\n\n".join(state["notes"])
    iteration = state["iterations"] + 1
    msg = _llm().invoke([
        HumanMessage(content=REFLECT_PROMPT.format(
            topic=state["topic"],
            notes=notes,
            iteration=iteration,
            max_iter=state["max_iterations"],
        )),
    ])
    data = _parse_json(msg.content, default={"done": True, "gaps": ""})
    done = bool(data.get("done", True))
    gaps = data.get("gaps", "") if not done else ""
    print(f"  ↳ reflection: done={done}, gaps={gaps[:120]!r}")
    return {"iterations": iteration, "reflection": gaps}


def should_continue(state: ResearchState) -> Literal["plan", "summarize"]:
    if state["iterations"] >= state["max_iterations"]:
        return "summarize"
    if not state.get("reflection"):
        return "summarize"
    return "plan"


SUMMARY_PROMPT = """Write a thorough, structured markdown research report.

Topic: {topic}

Iterative findings (in order, each round):
{notes}

Numbered source list (use these numbers in citations):
{sources}

Output this exact structure:

# Research Report: {topic}

## Executive Summary
3-5 sentences capturing the bottom line.

## Key Findings
Bulleted, each with inline citations [n]. Group related findings.

## Detailed Analysis
2-4 paragraphs synthesizing the evidence, noting agreements/disagreements.

## Community Perspectives (Reddit)
Highlight notable user sentiment, lived experiences, or recurring themes.
Omit this section if there were no useful Reddit findings.

## Open Questions / Limitations
What this research did not resolve.

## Sources
Numbered list of [n] Title — URL."""


def summarize_node(state: ResearchState) -> dict:
    notes = "\n\n---\n\n".join(state["notes"])
    lines = []
    idx = 1
    for r in state["web_results"]:
        lines.append(f"[{idx}] {r.get('title','')} — {r.get('url','')}")
        idx += 1
    for r in state["reddit_results"]:
        lines.append(f"[{idx}] (Reddit r/{r.get('subreddit','')}) {r.get('title','')} — {r.get('url','')}")
        idx += 1
    sources = "\n".join(lines) or "(none)"
    msg = _llm().invoke([
        HumanMessage(content=SUMMARY_PROMPT.format(
            topic=state["topic"], notes=notes, sources=sources,
        )),
    ])
    return {"final_report": msg.content}


def build_graph():
    g = StateGraph(ResearchState)
    g.add_node("plan", plan_node)
    g.add_node("search_web", search_web_node)
    g.add_node("search_reddit", search_reddit_node)
    g.add_node("analyze", analyze_node)
    g.add_node("reflect", reflect_node)
    g.add_node("summarize", summarize_node)

    g.add_edge(START, "plan")
    g.add_edge("plan", "search_web")
    g.add_edge("plan", "search_reddit")
    g.add_edge("search_web", "analyze")
    g.add_edge("search_reddit", "analyze")
    g.add_edge("analyze", "reflect")
    g.add_conditional_edges("reflect", should_continue, {
        "plan": "plan",
        "summarize": "summarize",
    })
    g.add_edge("summarize", END)
    return g.compile()
