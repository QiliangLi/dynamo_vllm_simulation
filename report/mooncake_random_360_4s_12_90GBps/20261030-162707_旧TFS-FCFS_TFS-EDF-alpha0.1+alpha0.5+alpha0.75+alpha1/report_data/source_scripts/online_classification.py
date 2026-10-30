"""Offline classification matching the controller state at computation time."""
from bisect import bisect_right
from collections import Counter, defaultdict
import json


class OnlineClassification:
    def __init__(self, status, config, gateway_origin, first_absolute, formal_ids):
        self.status = status
        self.records = {rid: status['records'][rid] for rid in sorted(formal_ids)}
        self.origin = gateway_origin
        self.first = first_absolute
        self.frozen = config['admission']['classification'] == 'arrival'
        self.thresholds = [event for event in status['events'] if event['event'] == 'threshold_updated']
        self.times = [event['t'] for event in self.thresholds]
        assert self.times == sorted(self.times)
        self.reservations = {event['id']: event for event in status['events']
                             if event['event'] == 'admission_reserved' and event.get('id') in self.records}
        self.layers = []
        self.seen = defaultdict(set)

    def threshold(self, absolute):
        index = bisect_right(self.times, absolute-self.origin)-1
        assert index >= 0
        return self.thresholds[index]

    def class_at(self, rid, absolute):
        if self.frozen:
            return 'heavy' if self.status['storage_hints'][rid]['is_heavy_read'] else 'light'
        theta = self.threshold(absolute)['theta']
        pressure = self.records[rid]['profile']['pressure_Bps']
        return 'heavy' if theta is not None and pressure > 0 and pressure >= theta else 'light'

    def record(self, mode, worker, step, layer, ids, absolute, duration):
        classes = {rid: self.class_at(rid, absolute) for rid in ids}
        group = set(classes.values())
        state = next(iter(group)) if len(group) == 1 else 'mixed'
        for rid, classification in classes.items():
            self.seen[rid].add(classification)
        threshold = self.threshold(absolute)
        self.layers.append(dict(mode=mode, worker=worker, step=step, layer=layer,
            start_s=absolute-self.first, model_s=duration, state=state,
            heavy_requests=sum(value == 'heavy' for value in classes.values()),
            light_requests=sum(value == 'light' for value in classes.values()),
            threshold_version=threshold['threshold_version'],
            theta_Bps=threshold['theta'],
            rule='arrival_frozen_hint' if self.frozen else 'dynamic_at_layer_compute_start',
            request_ids=json.dumps(ids, separators=(',', ':'))))
        return state

    def request_rows(self, mode):
        rows = []
        for rid, record in self.records.items():
            assert rid in self.seen
            decision = (self.status['storage_hints'][rid]['is_heavy_read'] if self.frozen
                        else self.reservations[rid]['heavy'])
            rows.append(dict(mode=mode, id=rid, historical_class=record['profile']['class_id'],
                admission_class='heavy' if decision else 'light',
                compute_classes='/'.join(sorted(self.seen[rid])),
                changed_while_computing=len(self.seen[rid]) > 1,
                pressure_Bps=record['profile']['pressure_Bps']))
        return rows

    def summary(self, mode):
        counts = Counter(layer['state'] for layer in self.layers)
        request_rows = self.request_rows(mode)
        return dict(mode=mode, rule='arrival_frozen_hint' if self.frozen else 'dynamic_at_layer_compute_start',
            admission_heavy=sum(row['admission_class'] == 'heavy' for row in request_rows),
            admission_light=sum(row['admission_class'] == 'light' for row in request_rows),
            compute_heavy_any=sum('heavy' in self.seen[rid] for rid in self.records),
            compute_light_any=sum('light' in self.seen[rid] for rid in self.records),
            changed_requests=sum(len(value) > 1 for value in self.seen.values()),
            heavy_layers=counts['heavy'], light_layers=counts['light'], mixed_layers=counts['mixed'],
            heavy_model_s=sum(row['model_s'] for row in self.layers if row['state'] == 'heavy'),
            light_model_s=sum(row['model_s'] for row in self.layers if row['state'] == 'light'),
            mixed_model_s=sum(row['model_s'] for row in self.layers if row['state'] == 'mixed'))
