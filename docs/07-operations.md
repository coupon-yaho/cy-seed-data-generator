# 운영 가이드

전송, 사전 점검, 적재, 검증. 그리고 멈췄을 때 뭘 볼지.

## 전송

```bash
rsync -avz -e "ssh -i $HOME/Downloads/키.pem" \
  --exclude '__pycache__' --exclude 'out' \
  ./seed/ ec2-user@<호스트>:~/seed/
```

`~`는 큰따옴표 안에서 확장되지 않으니 `$HOME`을 쓴다.
`.env`는 로컬에 없으므로 서버의 것이 덮어써지지 않는다.

## 사전 점검

```bash
python3 -V                # 3.9 이상
df -h /var/lib/docker     # 여유 3GB 이상
docker ps                 # MySQL 컨테이너 이름과 포트
pip3 install --user -r requirements.txt
```

`cryptography`만 필수다. 없으면 `--no-faker`와 `--load-mode docker`로 나머지를 우회한다.

## MySQL 설정

필수는 `local_infile` 하나뿐이고 재시작 없이 켤 수 있다.

```bash
docker exec -it <컨테이너> mysql -uroot -p -e "SET PERSIST local_infile=1;"
```

버퍼풀을 올리기 전에 컨테이너 메모리 한도를 먼저 봐야 한다.
`docker stats --no-stream`으로 현재 사용량이 한도에 가까우면 올리는 순간 OOM으로 죽는다.
여유가 있을 때만 조정한다.

```bash
docker exec -it <컨테이너> mysql -uroot -p -e "SET GLOBAL innodb_buffer_pool_size=536870912;"
```

새로 띄우는 경우이고 호스트 메모리가 넉넉하다면 기동 옵션으로 주는 게 낫다.

```bash
docker run -d --name coupon-mysql --memory=2g --cpus=2 \
  -e MYSQL_ROOT_PASSWORD=... -p 3306:3306 mysql:8.0 \
  --local-infile=1 --innodb-buffer-pool-size=768M \
  --innodb-redo-log-capacity=256M --innodb-flush-log-at-trx-commit=2 --skip-log-bin
```

`innodb_flush_log_at_trx_commit`은 GLOBAL 전용이라 세션에서 못 바꾼다.
시드는 세션 단위로 `foreign_key_checks`, `unique_checks`, `sql_log_bin`만 끈다.

## 키

```bash
cat > ~/seed/.env <<EOF
export AES_KEY=$(openssl rand -base64 32)
export HMAC_KEY=$(openssl rand -base64 32)
export SEED_DSN='mysql://root:실제비밀번호@127.0.0.1:3306/'
EOF
chmod 600 ~/seed/.env
source ~/seed/.env
```

키를 잃어버리면 적재한 100만 행을 복호화할 수 없다. Spring 앱도 같은 값을 쓴다.

DSN 비밀번호는 ASCII만 된다.
자리표시자를 그대로 두면 `UnicodeEncodeError: 'latin-1' codec can't encode`로 죽는다.
`@ : / # ?`가 있으면 URL 인코딩한다.

```bash
python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" 'p@ss:w0rd'
```

비밀번호를 모르면 컨테이너에서 확인할 수 있다.

```bash
docker inspect <컨테이너> --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -i MYSQL
```

## 실행

```bash
source ~/seed/.env

# 스모크 — 30초. 여기서 걸리면 본 실행도 걸린다
python3 bin/seed.py all      --dataset clean --scale 0.002 --schema seed_smoke
python3 bin/seed.py verify   --dataset clean --schema seed_smoke
python3 bin/seed.py teardown --schema seed_smoke

# 본 실행 — SSH 가 끊겨도 죽지 않게 tmux
tmux new -s seed
python3 bin/seed.py all --dataset clean   --schema coupon_clean
python3 bin/seed.py all --dataset corrupt --schema coupon_corrupt
python3 bin/seed.py verify --dataset clean   --schema coupon_clean   --chunk 100000
python3 bin/seed.py verify --dataset corrupt --schema coupon_corrupt --chunk 100000
```

MySQL 포트가 호스트에 노출돼 있지 않으면 모든 명령에
`--load-mode docker --container <이름>`을 붙인다.

통과 기준은 CLEAN이 `✓ CLEAN 0건 — 정상셋 성립`,
CORRUPT가 `✓ CORRUPT 집합 일치 — 누락 0 · 오탐 0`(expected 800 / 검출 800)이다.

## 디스크 예산

| 항목 | 크기 |
|---|---|
| CLEAN scale 1.0 | 1.81 GB |
| CORRUPT scale 0.2 | 0.34 GB |
| TSV 샤드 피크 | 0.13 GB |
| 제약 생성 임시파일, undo, redo | 0.6 GB |
| 합계 | 2.9 GB |

`--asof-state`는 300만 행(0.23GB)을 더한다.
`--keep-files`는 TSV 1.5GB를 남기므로 8GB 디스크에서는 쓰지 않는다.

여유가 5GB 이상이면 CORRUPT도 `--scale 1.0`으로 올릴 수 있다.
검출 정확도 판정에는 스케일이 영향을 주지 않는다.

## 멈춘 것 같을 때

```bash
docker exec -it <컨테이너> mysql -uroot -p -e "
  SELECT id, time, state, LEFT(info,60) FROM information_schema.processlist WHERE command<>'Sleep';
  SELECT trx_id, trx_rows_inserted, trx_state FROM information_schema.innodb_trx;"
df -h /                      # 임시파일이 디스크를 먹고 있는지
docker stats --no-stream     # 컨테이너가 메모리 한도에 붙어 있는지
```

`state`가 `Creating sort index`나 `Sending data`면 도는 중이고,
`trx_rows_inserted`가 늘고 있으면 진행 중이다.

`verify`가 Step 0에서 오래 걸리면 청크를 줄인다. 결과는 청크 크기와 무관하게 동일하다.

```bash
python3 bin/seed.py verify --dataset clean --schema coupon_clean --chunk 50000
```

## 흔한 오류

| 증상 | 원인과 해결 |
|---|---|
| `UnicodeEncodeError: 'latin-1' codec` | DSN 비밀번호에 비ASCII 문자. 키 절 참고 |
| `ERROR 1148 … LOAD DATA LOCAL` | `SET PERSIST local_infile=1` 안 함 |
| `ERROR 1049 Unknown database` | `--schema` 오타 또는 teardown 후 재실행 |
| `Warning: Identity file … not accessible` | pem 경로 오타 |
| 제약 생성에서 `Duplicate entry` | 시드가 보장하는 항목이라 정상적으로는 안 난다. 부분 적재를 의심하고 teardown 후 재실행 |
| 컨테이너가 죽음 | 버퍼풀을 메모리 한도 위로 올린 경우 |

## 재시작

```bash
python3 bin/seed.py teardown --schema coupon_clean
```

부분 적재 상태에서 `all`을 다시 돌려도 된다. `all`은 DROP 후 CREATE로 시작한다.
같은 `--seed`와 `--as-of`를 주면 이전과 같은 데이터가 나온다.
