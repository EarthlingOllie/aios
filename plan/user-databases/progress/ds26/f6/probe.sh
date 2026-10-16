. /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds26/f6/env.sh
P="psql -h /var/run/postgresql -p 5476 -U postgres -d aios_q6 -qAt"
$P -c "DELETE FROM users WHERE github_id=-1"
timeout 60 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_data_tools.py -k first_row > /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds26/f6/probe-pytest.txt 2>&1 &
sleep 25
ss -tnp | grep python
$P -c "select host,port,dbname from data_stores"
wait
