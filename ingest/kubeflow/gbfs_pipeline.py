"""Kubeflow Pipelines (KFP v2) version of the GBFS collector, for a local minikube cluster.

Two pipelines, each scheduled as a recurring run:
  * gbfs-poll          every 5 minutes: runs `gbfs_collector.py --once`
  * gbfs-health-check  daily at 07:00 UTC: runs `gbfs_health_check.py`
Both mount the same PersistentVolumeClaim (gbfs-data), so the Parquet files and the
change-detection state persist between runs, just like the data/ folder on a laptop.

Two settings matter for a poller and are easy to miss in an interview question:
  * caching is OFF. KFP caches a step whose inputs have not changed, and these steps
    have no inputs, so with caching on every run after the first would be skipped.
  * max_concurrency=1 and no_catchup=True. If the cluster was stopped, KFP will not
    fire hundreds of back-dated polls when it starts again (they could not fetch past
    data anyway), and two polls never run at the same time.

Usage (after the setup steps in ingest/README.md):
  python ingest/kubeflow/gbfs_pipeline.py compile
  python ingest/kubeflow/gbfs_pipeline.py schedule --host http://localhost:8080
  python ingest/kubeflow/gbfs_pipeline.py schedule --host http://localhost:8080 --postgres-secret gbfs-postgres
"""

from __future__ import annotations

import argparse
from pathlib import Path

from kfp import compiler, dsl, kubernetes

IMAGE = "bikeshare-gbfs:0.1"  # built into minikube, see Dockerfile
PVC_NAME = "gbfs-data"
HERE = Path(__file__).resolve().parent
POLL_YAML = HERE / "gbfs_poll_pipeline.yaml"
HEALTH_YAML = HERE / "gbfs_health_pipeline.yaml"
POSTGRES_SECRET: str | None = None  # set by --postgres-secret before compiling


@dsl.container_component
def poll_gbfs():
    return dsl.ContainerSpec(image=IMAGE, command=["python"],
                             args=["ingest/gbfs_collector.py", "--once", "--data-dir", "/data/gbfs"])


@dsl.container_component
def health_check():
    # --no-notify: there is no desktop inside a pod. A failed check exits 1, which
    # marks the run red in the Kubeflow UI, and the report is saved on the volume.
    return dsl.ContainerSpec(image=IMAGE, command=["python"],
                             args=["ingest/gbfs_health_check.py", "--data-dir", "/data/gbfs", "--no-notify"])


def _configure(task):
    task.set_caching_options(False)
    task.set_cpu_limit("500m").set_memory_limit("512Mi")
    kubernetes.mount_pvc(task, pvc_name=PVC_NAME, mount_path="/data")
    # The image exists only inside minikube; never try to pull it from a registry.
    kubernetes.set_image_pull_policy(task, "IfNotPresent")
    if POSTGRES_SECRET:
        # Secret with key POSTGRES_DSN, e.g. postgresql://user:pw@host.minikube.internal:5432/gbfs
        kubernetes.use_secret_as_env(task, secret_name=POSTGRES_SECRET,
                                     secret_key_to_env={"POSTGRES_DSN": "POSTGRES_DSN"})
    return task


@dsl.pipeline(name="gbfs-poll", description="Poll GBFS station_status/information once (change-only Parquet).")
def poll_pipeline():
    _configure(poll_gbfs())


@dsl.pipeline(name="gbfs-health-check", description="Daily check for missing GBFS snapshots.")
def health_pipeline():
    _configure(health_check())


def compile_all() -> None:
    compiler.Compiler().compile(poll_pipeline, str(POLL_YAML))
    compiler.Compiler().compile(health_pipeline, str(HEALTH_YAML))
    print(f"Wrote {POLL_YAML.name} and {HEALTH_YAML.name}")


def schedule(host: str, experiment: str) -> None:
    import kfp

    client = kfp.Client(host=host)
    exp = client.create_experiment(name=experiment)
    # KFP cron expressions have a seconds field first: "sec min hour day month weekday".
    jobs = [
        ("gbfs-poll-every-5-min", POLL_YAML, "0 */5 * * * *"),
        ("gbfs-health-check-daily", HEALTH_YAML, "0 0 7 * * *"),
    ]
    for name, path, cron in jobs:
        client.create_recurring_run(
            experiment_id=exp.experiment_id,
            job_name=name,
            pipeline_package_path=str(path),
            cron_expression=cron,
            max_concurrency=1,
            no_catchup=True,
            enable_caching=False,
            enabled=True,
        )
        print(f"Scheduled {name} ({cron})")


def main() -> None:
    global POSTGRES_SECRET
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["compile", "schedule"])
    parser.add_argument("--host", default="http://localhost:8080", help="Kubeflow Pipelines UI/API address")
    parser.add_argument("--experiment", default="bikeshare-gbfs")
    parser.add_argument("--postgres-secret", default=None,
                        help="name of a Kubernetes secret holding POSTGRES_DSN (optional)")
    args = parser.parse_args()
    POSTGRES_SECRET = args.postgres_secret
    compile_all()
    if args.action == "schedule":
        schedule(args.host, args.experiment)


if __name__ == "__main__":
    main()
