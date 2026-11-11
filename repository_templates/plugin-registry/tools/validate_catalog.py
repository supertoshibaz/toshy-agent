"""Validate metadata and downloaded ZIPs without importing plugin code."""
import argparse
import json
import sys
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('--app-root',type=Path,required=True)
parser.add_argument('--catalog',type=Path,default=Path('catalog.json'))
parser.add_argument('--owners',type=Path,default=Path('owners.json'))
parser.add_argument('--base-catalog',type=Path)
args=parser.parse_args()
sys.path.insert(0,str(args.app_root.resolve()))
from core.plugins.manifest import validate_catalog
from core.plugins.catalog import CatalogClient,github_parts
from tempfile import TemporaryDirectory
catalog=validate_catalog(json.loads(args.catalog.read_text()))
owners=json.loads(args.owners.read_text())
if not isinstance(owners,dict):raise SystemExit('owners.json must map namespace prefixes to GitHub owners.')
previous={}
if args.base_catalog and args.base_catalog.is_file():
 previous={p['id']:p for p in validate_catalog(json.loads(args.base_catalog.read_text()))['plugins']}
with TemporaryDirectory() as temp:
 client=CatalogClient(Path(temp)/'cache')
 for entry in catalog['plugins']:
  prefix=entry['id'].split('.')[0]
  allowed=owners.get(prefix,[])
  if isinstance(allowed,str):allowed=[allowed]
  owner=github_parts(entry['source'])[0]
  if owner.lower() not in [x.lower() for x in allowed]:raise SystemExit(f"Register/review namespace ownership before publishing {entry['id']}.")
  if previous.get(entry['id'])==entry:continue
  archive=entry['archive']
  package=client.download_package({'url':archive['url'],'sha256':archive['sha256'],'size':archive['size']},entry=entry)
  package.close()
  print(entry['id'],entry['version'],'verified')
print('Catalog verified. Structural validation is not a security audit.')
