"""Read-only source inventory; run as the existing central service identity."""
import json
from pathlib import Path
import argparse
from agentbridge.database.sources import Sources, connect
from psycopg import sql

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', required=True, type=Path)
    parser.add_argument('--source-id', required=True)
    args = parser.parse_args()
    sources = Sources(args.home)
    record = sources.get(args.source_id)
    if record['state'] != 'enabled' or record['active']['template_pack'] != 'taihua_logs':
        raise ValueError('This inventory requires an enabled taihua_logs source')
    config = record['active']
    with connect(sources, config) as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        tables = conn.execute("""SELECT table_name,column_name,data_type,is_nullable
          FROM information_schema.columns WHERE table_schema='analysis'
          ORDER BY table_name,ordinal_position""").fetchall()
        inventory = {}
        for col in tables:
            inventory.setdefault(col['table_name'], {'columns': []})['columns'].append(col)
        for name, value in inventory.items():
            relation = sql.Identifier('analysis', name)
            value['count'] = conn.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(relation)).fetchone()['n']
            columns = {c['column_name'] for c in value['columns']}
            if 'id' in columns:
                value['id_quality'] = conn.execute(sql.SQL('SELECT count(*)-count(DISTINCT id) AS duplicate_or_null_ids FROM {}').format(relation)).fetchone()
            if name == 'work_logs':
                value['profile'] = conn.execute(sql.SQL('SELECT min(log_date) AS first_date,max(log_date) AS last_date,count(DISTINCT user_id) AS people,count(DISTINCT project_id) AS projects,count(*) FILTER(WHERE project_id IS NULL) AS no_project,count(*) FILTER(WHERE hours IS NULL) AS no_hours,count(*) FILTER(WHERE hours<0) AS negative_hours FROM {}').format(relation)).fetchone()
                value['types'] = conn.execute(sql.SQL('SELECT type_code,count(*) AS logs,sum(hours) AS hours FROM {} GROUP BY type_code ORDER BY type_code').format(relation)).fetchall()
                value['years'] = conn.execute(sql.SQL('SELECT extract(year FROM log_date) AS year,count(*) AS logs FROM {} GROUP BY 1 ORDER BY 1').format(relation)).fetchall()
        relations = conn.execute('''SELECT
          (SELECT count(*) FROM analysis.work_logs l LEFT JOIN analysis.users u ON u.id=l.user_id WHERE u.id IS NULL) AS orphan_log_users,
          (SELECT count(*) FROM analysis.work_logs l LEFT JOIN analysis.projects p ON p.id=l.project_id WHERE l.project_id IS NOT NULL AND p.id IS NULL) AS orphan_log_projects,
          (SELECT count(*) FROM analysis.users u LEFT JOIN analysis.departments d ON d.id=u.dept_id WHERE u.dept_id IS NOT NULL AND d.id IS NULL) AS orphan_user_departments,
          (SELECT count(*) FROM analysis.work_log_comments c LEFT JOIN analysis.work_logs l ON l.id=c.work_log_id WHERE l.id IS NULL) AS orphan_comment_logs,
          (SELECT count(*) FROM analysis.work_log_comments c LEFT JOIN analysis.users u ON u.id=c.user_id WHERE u.id IS NULL) AS orphan_comment_users''').fetchone()
        quality = conn.execute('''SELECT count(*) FILTER(WHERE hours=0) AS zero_hours,
          count(*) FILTER(WHERE hours>24) AS over_24_hours,
          count(*) FILTER(WHERE content IS NULL OR btrim(content)='') AS empty_content,
          max(length(content)) AS max_content_length,
          count(*) FILTER(WHERE log_date IS NULL) AS null_dates,
          count(*) FILTER(WHERE user_id IS NULL) AS null_users
          FROM analysis.work_logs''').fetchone()
        daily_quality = conn.execute('''SELECT count(*) FILTER(WHERE n>1) AS multiple_log_person_days,
          count(*) FILTER(WHERE h>24) AS over_24_hour_person_days FROM
          (SELECT user_id,log_date,count(*) n,sum(hours) h FROM analysis.work_logs GROUP BY user_id,log_date) q''').fetchone()
        distributions = {}
        for name in inventory:
            distributions[name] = conn.execute(sql.SQL('SELECT status,count(*) AS n FROM {} GROUP BY status').format(sql.Identifier('analysis',name))).fetchall()
        channels = conn.execute('SELECT source_channel,count(*) AS n FROM analysis.work_logs GROUP BY source_channel').fetchall()
        duplicates = conn.execute('''SELECT
          (SELECT count(*) FROM (SELECT fullname FROM analysis.users GROUP BY fullname HAVING count(*)>1) q) AS duplicate_fullnames,
          (SELECT count(*) FROM (SELECT username FROM analysis.users GROUP BY username HAVING count(*)>1) q) AS duplicate_usernames,
          (SELECT count(*) FROM (SELECT name FROM analysis.projects GROUP BY name HAVING count(*)>1) q) AS duplicate_project_names''').fetchone()
        print(json.dumps({'source_id':args.source_id,'inventory':inventory,'relations':relations,
          'quality':quality,'daily_quality':daily_quality,'status_distributions':distributions,
          'channels':channels,'name_quality':duplicates},ensure_ascii=False,default=str,indent=2))
        conn.rollback()

if __name__ == '__main__':
    main()
