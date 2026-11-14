import json
import math
from .errors import missing, require
from .probe import compatible_audio
from .samples import read_pcm, describe_samples, silence_intervals, waveform_buckets
from .values import digest, canonical, identifier, now, object_body, integer, number


class AudioAnalysis:
    def __init__(self, db, assets, blobs, processes):
        self.db, self.assets, self.blobs, self.processes = db, assets, blobs, processes

    def _begin(self, asset_id, kind, settings):
        admission = self.assets.admit(asset_id)
        asset = admission["asset"]
        compatible_audio(asset["metadata"])
        return {
            "asset": asset,
            "kind": kind,
            "settings": dict(settings),
            "source_blob": admission["availability"]["blob"],
            "source_state": admission["availability"]["state"],
            "accepted_at": admission["accepted_at"],
        }

    def _complete(self, db, asset_id, kind, settings, admission):
        asset = self.assets.get(asset_id, db)
        require(admission["asset"]["id"] == asset_id,
                "analysis_source", "Analysis admission belongs to another source", 409)
        require(admission["kind"] == kind and admission["settings"] == settings,
                "analysis_request", "Analysis admission differs from the requested operation", 409)
        require(admission["source_blob"] == asset["blob"],
                "analysis_source", "Source content changed during analysis", 409)
        require(admission["source_state"] == "ready" and admission["accepted_at"] <= now(),
                "asset_retired", "Source media was unavailable when analysis was accepted", 409)
        # The handoff guard: a source retired while the measurement was still
        # running cannot register its result, even though admission was valid.
        require(asset["state"] == "ready",
                "asset_retired", "Source media retired before the analysis could be handed off", 409)
        return asset

    def normalize(self, kind, raw):
        if kind == "loudness":
            object_body(raw, ())
            return {}
        if kind == "signal":
            object_body(raw, ("threshold_db", "minimum_silence"))
            return {"threshold_db": number(raw.get("threshold_db", -40), "threshold_db", -100, -1),
                    "minimum_silence": number(raw.get("minimum_silence", .2), "minimum_silence", .01, 60)}
        require(kind == "waveform", "analysis_kind", "Unknown audio analysis")
        object_body(raw, ("buckets",))
        return {"buckets": integer(raw.get("buckets", 200), "buckets", 1, 2000)}

    def run(self, asset_id, kind, raw):
        settings = self.normalize(kind, raw)
        admission = self._begin(asset_id, kind, settings)
        asset = admission["asset"]
        fingerprint = digest({"blob": asset["blob"], "kind": kind, "settings": settings, "algorithm": 1})
        with self.db.read() as db:
            existing = db.execute(
                "SELECT id FROM analyses WHERE fingerprint=? AND state='valid'",
                (fingerprint,)).fetchone()
            if existing:
                return self.get(existing["id"], db)
        if kind == "loudness":
            result = self.measure_loudness(asset)
            return self.persist(asset_id, kind, settings, result, fingerprint, admission)
        rate = 16000
        with self.blobs.temporary(".pcm") as pcm:
            self.processes.decode_audio(self.blobs.resolve(asset["blob"], verify=True), pcm, rate)
            samples = read_pcm(pcm)
            require(len(samples) > 0, "empty_audio", "Decoded audio is empty")
            if kind == "signal":
                result = {**describe_samples(samples, rate),
                          "silence": silence_intervals(samples, rate, settings["threshold_db"], settings["minimum_silence"]),
                          "units": "dBFS", "note": "PCM peak/RMS; not EBU R128 integrated loudness"}
            else:
                result = {"sample_rate": rate, "sample_count": len(samples),
                          "buckets": waveform_buckets(samples, settings["buckets"])}
        return self.persist(asset_id, kind, settings, result, fingerprint, admission)

    def persist(self, asset_id, kind, settings, result, fingerprint, admission):
        with self.db.write_unit() as db:
            self._complete(db, asset_id, kind, settings, admission)
            row = db.execute(
                "SELECT id,state FROM analyses WHERE fingerprint=?", (fingerprint,)).fetchone()
            if row:
                # A row invalidated by an earlier retirement window can become
                # effective again once the source is restored and the same
                # measurement is re-run. Re-registering moves its timestamp to
                # the new acceptance instant so later reconciliations judge it
                # against the current lifecycle rather than the old window.
                if row["state"] != "valid":
                    revalidated_at = now()
                    db.execute("UPDATE analyses SET state='valid',created=? WHERE id=?",
                               (revalidated_at, row["id"]))
                    self.db.event(db, asset_id, "analysis_revalidated",
                                  {"analysis": row["id"], "kind": kind, "created": revalidated_at})
                return self.get(row["id"], db)
            analysis_id = identifier("analysis")
            db.execute(
                "INSERT INTO analyses(id,asset,kind,settings,result,fingerprint,created,state) "
                "VALUES(?,?,?,?,?,?,?,'valid')",
                (analysis_id, asset_id, kind, canonical(settings), canonical(result),
                 fingerprint, now()))
            self.db.event(db, asset_id, "analyzed", {"analysis": analysis_id, "kind": kind})
            return self.get(analysis_id, db)

    def measure_loudness(self, asset):
        with self.blobs.temporary(".null") as output:
            completed = self.processes.run([
                "-loglevel", "info", "-protocol_whitelist", "file,pipe",
                "-i", str(self.blobs.resolve(asset["blob"], verify=True)),
                "-map", "0:a:0", "-vn", "-af", "loudnorm=I=-23:TP=-1:LRA=7:print_format=json",
                "-f", "null", str(output)])
        log = completed["stderr"]
        start, end = log.rfind("{"), log.rfind("}")
        require(start >= 0 and end > start, "loudness_report", "Encoder did not return loudness measurements")
        try:
            measured = json.loads(log[start:end+1])
        except ValueError as exc:
            from .errors import LoomError
            raise LoomError("loudness_report", "Loudness measurements could not be decoded") from exc
        result = {"standard": "EBU R128", "units": "LUFS/dBTP", "silence": False}
        mapping = {"input_i": "integrated_lufs", "input_tp": "true_peak_dbtp",
                   "input_lra": "range_lu", "input_thresh": "threshold_lufs"}
        for source, target in mapping.items():
            value = float(measured[source])
            result[target] = round(value, 3) if math.isfinite(value) else None
        result["silence"] = result["integrated_lufs"] is None
        return result

    def get(self, analysis_id, db=None):
        if db is None:
            with self.db.read() as read:
                return self.get(analysis_id, read)
        row = db.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
        if row is None or row["state"] != "valid":
            # Invalidated handoffs are retained for audit but never served as
            # effective measurements.
            raise missing("analysis", analysis_id)
        return {**dict(row), "settings": json.loads(row["settings"]), "result": json.loads(row["result"])}

    def list(self, asset_id):
        with self.db.read() as db:
            rows = db.execute(
                "SELECT id FROM analyses WHERE asset=? AND state='valid' ORDER BY created,id",
                (asset_id,)).fetchall()
            return [self.get(row["id"], db) for row in rows]

    def reconcile(self):
        """Classify analysis rows against retirement history at startup.

        A measurement is only effective if its source was ready at the moment
        the result was registered. Rows registered while the source was
        retired are invalidated; they remain on disk for audit but are
        excluded from every read path. Legal historical measurements,
        including those of currently retired sources, stay valid.
        """
        invalidated, retained = [], 0
        with self.db.write_unit() as db:
            rows = db.execute("SELECT id,asset,created FROM analyses ORDER BY asset,created").fetchall()
            for row in rows:
                # Sequence orders the lifecycle authoritatively; an event at
                # the same wall-clock instant as the registration is treated
                # as already in effect (the retire/persist write transactions
                # are serialized, so an exact tie only occurs in test setups).
                lifecycle = db.execute(
                    "SELECT kind,created FROM events "
                    "WHERE subject=? AND kind IN ('retired','restored') ORDER BY sequence",
                    (row["asset"],)).fetchall()
                ready_at_registration = True
                for event in lifecycle:
                    if event["created"] > row["created"]:
                        break
                    ready_at_registration = event["kind"] == "restored"
                if ready_at_registration:
                    retained += 1
                    continue
                current = db.execute("SELECT state FROM analyses WHERE id=?", (row["id"],)).fetchone()
                if current is not None and current["state"] != "invalidated":
                    db.execute("UPDATE analyses SET state='invalidated' WHERE id=?", (row["id"],))
                    self.db.event(db, row["asset"], "analysis_invalidated", {"analysis": row["id"]})
                    invalidated.append(row["id"])
        return {"valid": retained, "invalidated": invalidated}
