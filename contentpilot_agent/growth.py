"""Evidence-based reporting. Cumulative snapshots are never added together."""

from sqlalchemy import select

from .database import DataCollection, HumanEvaluation, PerformanceSnapshot, Publication


def ratios(impressions, clicks, conversions, revenue, cost):
    return {
        "ctr": round(clicks / impressions, 6) if impressions else None,
        "conversion_rate": round(conversions / clicks, 6) if clicks else None,
        "roi": round((revenue - cost) / cost, 6) if cost else None,
        "roas": round(revenue / cost, 6) if cost else None,
    }


def publication_data(session, publication):
    observation = session.scalar(
        select(DataCollection)
        .where(
            DataCollection.publication_id == publication.id, DataCollection.status == "completed"
        )
        .order_by(DataCollection.created_at.desc())
        .limit(1)
    )
    latest = session.scalar(
        select(PerformanceSnapshot)
        .where(PerformanceSnapshot.publication_id == publication.id)
        .order_by(PerformanceSnapshot.captured_at.desc(), PerformanceSnapshot.created_at.desc())
        .limit(1)
    )
    metrics = None
    if latest:
        metrics = {
            key: getattr(latest, key)
            for key in (
                "impressions",
                "clicks",
                "conversions",
                "revenue",
                "cost",
                "evidence",
                "captured_at",
            )
        }
        metrics.update(
            ratios(
                latest.impressions, latest.clicks, latest.conversions, latest.revenue, latest.cost
            )
        )
    return {
        **{
            key: getattr(publication, key)
            for key in (
                "id",
                "job_id",
                "content_index",
                "revision",
                "platform",
                "content",
                "source",
                "experiment",
                "variant",
                "channel",
                "status",
                "url",
                "error",
                "created_at",
            )
        },
        "metrics": metrics,
        "collected_metrics": observation.observation if observation else None,
    }


def evaluation_data(evaluation):
    result = {
        key: getattr(evaluation, key)
        for key in (
            "id",
            "job_id",
            "content_index",
            "revision",
            "facts_score",
            "platform_score",
            "usefulness_score",
            "factual_errors",
            "note",
            "created_at",
        )
    }
    result["passed"] = (
        min(evaluation.facts_score, evaluation.platform_score, evaluation.usefulness_score) >= 4
        and not evaluation.factual_errors
    )
    return result


def latest_evaluations(session, owner_id):
    values = session.scalars(
        select(HumanEvaluation)
        .where(HumanEvaluation.owner_id == owner_id)
        .order_by(HumanEvaluation.created_at.desc())
    )
    seen, result = set(), []
    for value in values:
        key = (value.job_id, value.revision, value.content_index)
        if key not in seen:
            seen.add(key)
            result.append(evaluation_data(value))
    return result


def performance_context(session, owner_id, product_id):
    values = session.scalars(
        select(Publication)
        .where(Publication.owner_id == owner_id, Publication.status == "published")
        .order_by(Publication.created_at.desc())
        .limit(200)
    )
    candidates = []
    for pub in values:
        if pub.source.get("id") != product_id:
            continue
        data = publication_data(session, pub)
        m = data["metrics"]
        if m and m["impressions"]:
            candidates.append(
                {
                    "publication_id": pub.id,
                    "platform": pub.platform,
                    "title": pub.content["title"],
                    "experiment": pub.experiment,
                    "variant": pub.variant,
                    "metrics": m,
                    "interpretation": "录入或采集的观察数据；仅作为下一轮选题假设，不代表因果结论",
                }
            )
        elif data.get("collected_metrics"):
            candidates.append(
                {
                    "publication_id": pub.id,
                    "platform": pub.platform,
                    "title": pub.content["title"],
                    "experiment": pub.experiment,
                    "variant": pub.variant,
                    "metrics": {**data["collected_metrics"], "ctr": None},
                    "interpretation": "自动采集的部分指标；缺失值未知，不能推算曝光、成交或因果关系",
                }
            )
    candidates.sort(key=lambda c: c["metrics"]["ctr"] or 0, reverse=True)
    return candidates[:10]


def growth_report(session, owner_id):
    publications = list(
        session.scalars(
            select(Publication)
            .where(Publication.owner_id == owner_id)
            .order_by(Publication.created_at.desc())
            .limit(1000)
        )
    )
    rows = [publication_data(session, p) for p in publications]
    observed = [p for p in rows if p["status"] == "published" and p["metrics"]]
    totals = {
        key: sum(p["metrics"][key] for p in observed)
        for key in ("impressions", "clicks", "conversions", "revenue", "cost")
    }
    totals.update(ratios(**totals))
    groups = {}
    for pub in observed:
        # Compare variants only inside the same experiment AND platform.
        if not pub["experiment"]:
            continue
        key = (pub["experiment"], pub["platform"], pub["variant"])
        group = groups.setdefault(
            key,
            {
                "experiment": key[0],
                "platform": key[1],
                "variant": key[2],
                "samples": 0,
                **dict.fromkeys(totals.keys(), 0),
            },
        )
        group["samples"] += 1
        for name in ("impressions", "clicks", "conversions", "revenue", "cost"):
            group[name] += pub["metrics"][name]
    for group in groups.values():
        group.update(
            ratios(
                **{k: group[k] for k in ("impressions", "clicks", "conversions", "revenue", "cost")}
            )
        )
    labels = latest_evaluations(session, owner_id)
    evaluation = {
        "sample_size": len(labels),
        "pass_rate": sum(e["passed"] for e in labels) / len(labels) if labels else None,
        "factual_error_rate": sum(bool(e["factual_errors"]) for e in labels) / len(labels)
        if labels
        else None,
        "source": "人工标注；每个内容版本采用最新一次标注，非模型自评分",
    }
    return {
        "publications": rows,
        "observed_samples": len(observed),
        "totals": totals,
        "experiments": list(groups.values()),
        "evaluation": evaluation,
        "limit": 1000,
        "truncated": len(rows) == 1000,
        "caveat": "观察性对比，不是随机分流 A/B 实验；ROI 为营收减录入总成本除以总成本，非净利润。",
    }
