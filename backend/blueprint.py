import os
from datetime import datetime
import logging
from sqlalchemy.orm.exc import NoResultFound
from flask import (
    Blueprint,
    request,
    current_app,
    send_from_directory)
from flask_cors import cross_origin
from geonature.utils.utilssqlalchemy import (
    json_resp, to_json_resp, to_csv_resp)
from geonature.utils.utilstoml import load_toml
from geonature.utils.filemanager import (
    removeDisallowedFilenameChars, delete_recursively)
from pypnusershub.db.tools import InsufficientRightsError
from pypnusershub import routes as fnauth

from .repositories import ExportRepository, EmptyDataSetError


logger = current_app.logger
logger.setLevel(logging.DEBUG)
# logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)
# current_app.config['DEBUG'] = True

blueprint = Blueprint('exports', __name__)
repo = ExportRepository()


EXPORTS_DIR = os.path.join(current_app.static_folder, 'exports')
os.makedirs(EXPORTS_DIR, exist_ok=True)
SHAPEFILES_DIR = os.path.join(current_app.static_folder, 'shapefiles')
MOD_CONF_PATH = os.path.join(blueprint.root_path, os.pardir, 'config')
MOD_CONF = load_toml(os.path.join(MOD_CONF_PATH, 'conf_gn_module.toml'))
ID_MODULE, API_URL = (MOD_CONF.get(k) for k in ('id_application', 'api_url'))
ASSETS = os.path.join(blueprint.root_path, 'assets')

# extracted from dummy npm install
SWAGGER_UI_DIST_DIR = os.path.join(ASSETS, 'swagger-ui-dist')
SWAGGER_UI_SAMPLE_INDEXHTML = 'swagger-ui_index.template.html'
SWAGGER_UI_INDEXHTML = 'index.html'
SWAGGER_API_SAMPLE_YAML = 'swagger-ui_api.template.yml'
SWAGGER_API_YAML = 'api.yml'

for template, serving in {
        os.path.join(
            MOD_CONF_PATH, SWAGGER_API_SAMPLE_YAML): os.path.join(
                ASSETS, SWAGGER_API_YAML),
        os.path.join(
            MOD_CONF_PATH, SWAGGER_UI_SAMPLE_INDEXHTML): os.path.join(
                SWAGGER_UI_DIST_DIR, SWAGGER_UI_INDEXHTML)
        }.items():
    with open(template, 'r') as input_:
        content = input_.read()
        host, base_path, *_ = current_app.config['API_ENDPOINT']\
                                         .replace('https://', '')\
                                         .replace('http://', '')\
                                         .split('/', 1) + ['']
        for k, v in ({
                'API_ENDPOINT': current_app.config['API_ENDPOINT'],
                'HOST': host,
                'BASE_PATH': '/' + base_path if base_path else '',
                'API_URL': API_URL.lstrip('/') if API_URL else '',
                'API_YAML': SWAGGER_API_YAML
                }).items():
            content = content.replace('{{{{{}}}}}'.format(k), v)
        with open(serving, 'w') as output:
            output.write(content)


@blueprint.route('/swagger-ui/')
def swagger_ui():
    return send_from_directory(SWAGGER_UI_DIST_DIR, 'index.html')


@blueprint.route('/swagger-ui/<asset>')
def swagger_assets(asset):
    return send_from_directory(SWAGGER_UI_DIST_DIR, asset)


@blueprint.route('/' + SWAGGER_API_YAML)
def swagger_api_yml():
    return send_from_directory(ASSETS, SWAGGER_API_YAML)


def export_filename(export):
    return '{}_{}'.format(
        removeDisallowedFilenameChars(export.get('label')),
        datetime.now().strftime('%Y_%m_%d_%Hh%Mm%S'))


@blueprint.route('/<int:id_export>/<export_format>', methods=['GET'])
@cross_origin(
    supports_credentials=True,
    allow_headers=['content-type', 'content-disposition'],
    expose_headers=['Content-Type', 'Content-Disposition', 'Authorization'])
@fnauth.check_auth_cruved(
    'E', True, id_app=ID_MODULE,
    redirect_on_expiration=current_app.config.get('URL_APPLICATION'),
    redirect_on_invalid_token=current_app.config.get('URL_APPLICATION'))
def getOneExport(id_export, export_format, info_role):
    if (id_export < 1
            or export_format not in blueprint.config.get('export_format_map')):
        return to_json_resp({'api_error': 'InvalidExport'}, status=404)

    current_app.config.update(
        export_format_map=blueprint.config['export_format_map'])
    filters = {f: request.args.get(f) for f in request.args}
    try:
        export, columns, data = repo.get_by_id(
            info_role, id_export, with_data=True, export_format=export_format,
            filters=filters, limit=10000, offset=0)

        if export:
            fname = export_filename(export)
            has_geometry = export.get('geometry_field', None)

            if export_format == 'json':
                return to_json_resp(
                    data.get('items'),
                    as_file=True,
                    filename=fname,
                    indent=4)

            if export_format == 'csv':
                return to_csv_resp(
                    fname,
                    data.get('items'),
                    [c.name for c in columns],
                    separator=',')

            if (export_format == 'shp' and has_geometry):
                from geojson.geometry import Point, Polygon, MultiPolygon
                from geonature.utils.utilsgeometry import FionaShapeService as ShapeService  # noqa: E501

                delete_recursively(
                    SHAPEFILES_DIR, excluded_files=['.gitkeep'])

                ShapeService.create_shapes_struct(
                    db_cols=columns, srid=export.get('geometry_srid'),
                    dir_path=SHAPEFILES_DIR, file_name=''.join(['export_', fname]))  # noqa: E501

                items = data.get('items')
                for feature in items['features']:
                    geom, props = (feature.get(field)
                                   for field in ('geometry', 'properties'))
                    if isinstance(geom, Point):
                        ShapeService.point_shape.write(feature)
                        ShapeService.point_feature = True

                    elif (isinstance(geom, Polygon)
                            or isinstance(geom, MultiPolygon)):
                        ShapeService.polygone_shape.write(props)
                        ShapeService.polygon_feature = True

                    else:
                        ShapeService.polyline_shape.write(props)
                        ShapeService.polyline_feature = True

                ShapeService.save_and_zip_shapefiles()

                return send_from_directory(
                    SHAPEFILES_DIR, ''.join(['export_', fname, '.zip']),
                    as_attachment=True)

            else:
                return to_json_resp(
                    {'api_error': 'NonTransformableError'}, status=404)

    except NoResultFound as e:
        return to_json_resp(
            {'api_error': 'NoResultFound',
             'message': str(e)}, status=404)
    except InsufficientRightsError:
        return to_json_resp(
            {'api_error': 'InsufficientRightsError'}, status=403)
    except EmptyDataSetError as e:
        return to_json_resp(
            {'api_error': 'EmptyDataSetError',
             'message': str(e)}, status=404)
    except Exception as e:
        logger.critical('%s', e)
        if current_app.config['DEBUG']:
            raise
        return to_json_resp({'api_error': 'LoggedError'}, status=400)


@blueprint.route('/', methods=['GET'])
@fnauth.check_auth_cruved(
    'R', True, id_app=ID_MODULE,
    redirect_on_expiration=current_app.config.get('URL_APPLICATION'),
    redirect_on_invalid_token=current_app.config.get('URL_APPLICATION'))
@json_resp
def getExports(info_role):
    try:
        exports = repo.getAllowedExports(info_role)
    except NoResultFound:
        return {'api_error': 'NoResultFound',
                'message': 'Configure one or more export'}, 404
    except Exception as e:
        logger.critical('%s', str(e))
        return {'api_error': 'LoggedError'}, 400
    else:
        return [export.as_dict() for export in exports]


@blueprint.route('/etalab', methods=['GET'])
def etalab_export():
    from datetime import time
    from geonature.utils.env import DB
    from geonature.utils.utilssqlalchemy import GenericQuery
    from .rdf import OccurrenceStore

    conf = current_app.config.get('exports')
    export_etalab = conf.get('etalab_export')
    seeded = False
    if os.path.isfile(export_etalab):
        seeded = True
        midnight = datetime.combine(datetime.today(), time.min)
        mtime = datetime.fromtimestamp(os.path.getmtime(export_etalab))
        ts_delta = mtime - midnight
    if not seeded or ts_delta.total_seconds() < 0:
        store = OccurrenceStore()
        query = GenericQuery(
            DB.session, 'export_occtax_sinp', 'pr_occtax',
            geometry_field=None, filters=[])
        data = query.return_query()
        for record in data.get('items'):
            event = store.build_event(record)
            obs = store.build_human_observation(event, record)
            store.build_location(obs, record)
            occurrence = store.build_occurrence(event, record)
            organism = store.build_organism(occurrence, record)
            identification = store.build_identification(organism, record)
            store.build_taxon(identification, record)
        store.save(store_uri=''.join(['file://', export_etalab]))

    return send_from_directory(
        os.path.dirname(export_etalab), os.path.basename(export_etalab))
