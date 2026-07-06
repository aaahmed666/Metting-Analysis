from __future__ import annotations
from typing import Any

PERFORMANCE_REPORT_SYSTEM = """\
You are a Senior Sales Performance Analyst AI.
Analyze the provided employee data and return a JSON object with this schema:
{
  "executive_summary": "Summary of overall performance.",
  "performance_level": "Excellent | Good | Average | Below Average | Poor",
  "strengths": ["list of strengths"],
  "areas_for_improvement": ["list of weaknesses"],
  "skill_analysis": {
    "discovery": "Analysis of discovery score.",
    "objection": "Analysis of objection handling.",
    "closing": "Analysis of closing score.",
    "listening": "Analysis of active listening.",
    "next_steps": "Analysis of next steps clarity."
  },
  "meeting_activity_analysis": "Commentary on uploads and status.",
  "talk_ratio_analysis": "Commentary on speaking time ratio.",
  "grade_trend_analysis": "Commentary on grade consistency.",
  "recommendations": [
    {
      "priority": "high | medium | low",
      "action": "action item",
      "reason": "rationale"
    }
  ],
  "overall_assessment": "Final summary statement."
}

Rules:
- Output valid JSON only.
- Base every statement on the provided numbers.
- Talk ratio range 0.30 - 0.50 is healthy.
- Grade scale: A (90-100), B (75-89), C (60-74), D (below 60).
"""


def build_report_prompt(data: dict[str, Any]) -> str:
    profile       = data.get("profile", {})
    team_info     = data.get("team_info", {})
    meeting_stats = data.get("meeting_stats", {})
    kpis          = data.get("kpis", {})
    avg_scores    = kpis.get("avg_scores", {})
    grade_dist    = kpis.get("grade_distribution", {})
    by_status     = meeting_stats.get("by_status", {})
    recent        = data.get("recent_meetings", [])

    def fmt(val: Any, suffix: str = "") -> str:
        return f"{val}{suffix}" if val is not None else "N/A"

    def fmt_ratio(val: Any) -> str:
        return f"{round(float(val) * 100, 1)}%" if val is not None else "N/A"

    def fmt_grades(gd: dict) -> str:
        if not gd:
            return "No graded meetings."
        return " | ".join([f"{g}: {c}" for g, c in sorted(gd.items())])

    def fmt_recent(meetings: list) -> str:
        if not meetings:
            return "No recent completed meetings."
        lines = []
        for i, m in enumerate(meetings[:5], 1):
            score = fmt(m.get("total_score"), "/100")
            grade = m.get("grade", "N/A")
            date  = (m.get("meeting_date") or "N/A")[:10]
            lines.append(f"{i}. Date: {date} | Score: {score} | Grade: {grade}")
        return "\n".join(lines)

    prompt = f"""
Employee: {profile.get("full_name", "Unknown")} ({profile.get("email", "N/A")})
Role: {profile.get("role", "sales_rep")}
Member Since: {str(profile.get("created_at", "N/A"))[:10]}
Team: {team_info.get("team_name", "N/A")} | Manager: {team_info.get("manager_name", "N/A")}

Activity Stats:
- Total Meetings: {fmt(meeting_stats.get("total_meetings"))}
- Completed: {fmt(by_status.get("completed"))}
- Pending: {fmt(by_status.get("pending"))}
- Processing: {fmt(by_status.get("processing"))}
- Rejected: {fmt(by_status.get("rejected"))}
- Completion Rate: {fmt(meeting_stats.get("completion_rate"), "%")}
- Rejection Rate: {fmt(meeting_stats.get("rejection_rate"), "%")}

Performance KPIs:
- Average Score: {fmt(kpis.get("avg_score"), " / 100")}
- Average Talk Ratio: {fmt_ratio(kpis.get("avg_talk_ratio"))}
- Grade Distribution: {fmt_grades(grade_dist)}

Skill Averages:
- Discovery: {fmt(avg_scores.get("discovery"), " / 100")}
- Objection Handling: {fmt(avg_scores.get("objection"), " / 100")}
- Closing: {fmt(avg_scores.get("closing"), " / 100")}
- Listening: {fmt(avg_scores.get("listening"), " / 100")}
- Next Steps: {fmt(avg_scores.get("next_steps"), " / 100")}

Recent Meetings:
{fmt_recent(recent)}

Task: Write a performance report based on the data above.
"""
    return prompt
