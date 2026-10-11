ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION}-slim

ARG H2_VERSION=2.5.252
ENV JAVA_HOME=/opt/java/openjdk \
    H2_JAR=/opt/h2/h2.jar \
    MPLBACKEND=Agg \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
ENV PATH="${JAVA_HOME}/bin:${PATH}"

COPY --from=eclipse-temurin:21-jre /opt/java/openjdk /opt/java/openjdk

RUN python - <<PY
import hashlib, pathlib, urllib.request
base = "https://repo1.maven.org/maven2/com/h2database/h2/${H2_VERSION}/h2-${H2_VERSION}.jar"
data = urllib.request.urlopen(base, timeout=60).read()
expected = urllib.request.urlopen(base + ".sha1", timeout=60).read().decode().split()[0].strip()
actual = hashlib.sha1(data).hexdigest()
assert actual == expected, (actual, expected)
path = pathlib.Path("${H2_JAR}")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_bytes(data)
print("h2", "${H2_VERSION}", "sha1 ok", actual)
PY

WORKDIR /lab
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY isolation_lab ./isolation_lab
COPY tests ./tests

CMD ["python", "-m", "isolation_lab", "--journal", "logs/run.jsonl"]
