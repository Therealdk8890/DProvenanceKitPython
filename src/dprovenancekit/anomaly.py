"""Rule-based anomaly and regression detection over recorded runs."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List

from .live_engine import TraceQuerySubscription
from .query import TraceQueryDSL, TraceRun


@dataclass(frozen=True)
class Anomaly:
    run_id: uuid.UUID
    rule_name: str
    description: str


class AnomalyRule(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def anomaly_query(self) -> TraceQueryDSL:
        """The query that identifies an anomalous run."""

    @abstractmethod
    def describe(self, run: TraceRun) -> str: ...

    def make_anomaly(self, run: TraceRun) -> Anomaly:
        return Anomaly(run_id=run.run_id, rule_name=self.name, description=self.describe(run))


class AnomalyDetector:
    def __init__(self, store):
        self.store = store

    def detect_anomalies(self, rules: List[AnomalyRule]) -> List[Anomaly]:
        anomalies: List[Anomaly] = []
        for rule in rules:
            anomalous_runs = self.store.query_runs(rule.anomaly_query)
            for run in anomalous_runs:
                anomalies.append(rule.make_anomaly(run))
        return anomalies

    def register_live(self, rules: List[AnomalyRule], live_engine) -> None:
        for rule in rules:
            live_engine.register(LiveAnomalySubscription(rule))


class LiveAnomalySubscription(TraceQuerySubscription):
    def __init__(self, rule: AnomalyRule):
        self.query_id = uuid.uuid4()
        self.rule = rule

    @property
    def query(self) -> TraceQueryDSL:
        return self.rule.anomaly_query

    def on_match(self, run: TraceRun) -> None:
        anomaly = self.rule.make_anomaly(run)
        print(
            f"🚨 LIVE ANOMALY DETECTED: [{anomaly.rule_name}] "
            f"{anomaly.description} in run {anomaly.run_id}"
        )

    def on_update(self, run: TraceRun) -> None:
        pass
