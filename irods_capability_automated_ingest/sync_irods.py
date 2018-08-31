import os
from os.path import dirname, getsize, getmtime, basename, abspath
from irods.session import iRODSSession
from irods.models import Resource, DataObject, Collection
from .sync_utils import size, get_redis, call, get_hdlr_mod
from .utils import Operation
import redis_lock
import json
import irods.keywords as kw
import base64, random


def child_of(session, child_resc_name, resc_name):
    if child_resc_name == resc_name:
        return True
    else:
        while True:
            child_resc = session.resources.get(child_resc_name)
            parent_resc_id = child_resc.parent
            if parent_resc_id is None:
                break

            parent_resc_name = None
            for row in session.query(Resource.name).filter(Resource.id == parent_resc_id):
                parent_resc_name = row[Resource.name]
            if parent_resc_name == resc_name:
                return True
            child_resc_name = parent_resc_name
        return False


def create_dirs(hdlr_mod, logger, session, meta, **options):
    target = meta["target"]
    path = meta["path"]
    config = meta["config"]
    if target.startswith("/"):
        r = get_redis(config)
        if not session.collections.exists(target):
            with redis_lock.Lock(r, "create_dirs:" + path):
                if not session.collections.exists(target):
                    if target == "/":
                        raise Exception("create_dirs: Cannot create root")
                    meta2 = meta.copy()
                    meta2["target"] = dirname(target)
                    meta2["path"] = dirname(path)
                    create_dirs(hdlr_mod, logger, session, meta2, **options)

                    call(hdlr_mod, "on_coll_create", create_dir, logger, hdlr_mod, logger, session, meta, **options)
    else:
        raise Exception("create_dirs: relative path")


def create_dir(hdlr_mod, logger, session, meta, **options):
    target = meta["target"]
    path = meta["path"]
    logger.info("creating collection " + target)
    session.collections.create(target)


def get_target_path(hdlr_mod, session, meta, **options):
    if hasattr(hdlr_mod, "target_path"):
        return hdlr_mod.target_path(session, meta, **options)
    else:
        return None


def get_resource_name(hdlr_mod, session, meta, **options):
    if hasattr(hdlr_mod, "to_resource"):
        return hdlr_mod.to_resource(session, meta, **options)
    else:
        return None


def register_file(hdlr_mod, logger, session, meta, **options):
    dest_data_obj_path = meta["target"]
    source_file_path = meta["path"]

    phypath_to_register = get_target_path(hdlr_mod, session, meta, **options)
    if phypath_to_register is None:
        if 'unicode_error_filename' in meta:
            phypath_to_register = os.path.join(source_file_path, meta['unicode_error_filename'])
        else:
            phypath_to_register = source_file_path

    resc_name = get_resource_name(hdlr_mod, session, meta, **options)

    if resc_name is not None:
        options["destRescName"] = resc_name

    if 'b64_path_str' in meta:
        b64str = meta['b64_path_str']
        b64decoded_utf8_bstr = base64.b64decode(b64str)
        source_file_path = b64decoded_utf8_bstr

    size = getsize(source_file_path)
    mtime = int(getmtime(source_file_path))
    options[kw.DATA_SIZE_KW] = str(size)
    options[kw.DATA_MODIFY_KW] = str(mtime)

    logger.info("registering object " + dest_data_obj_path + ", options = " + str(options))
    session.data_objects.register(phypath_to_register, dest_data_obj_path, **options)

    logger.info("succeeded", task="irods_register_file", path = source_file_path)

    if 'b64_path_str' in meta:
        obj = session.data_objects.get(meta['target'])
        obj.metadata.add("irods::automated_ingest::UnicodeEncodeError", meta['b64_path_str'], 'python3.base64.b64encode(full_path_of_source_file)')

def upload_file(hdlr_mod, logger, session, meta, **options):
    dest_data_obj_path = meta["target"]
    source_file_path = meta["path"]
    resc_name = get_resource_name(hdlr_mod, session, meta, **options)

    if resc_name is not None:
        options["destRescName"] = resc_name

    logger.info("uploading object " + dest_data_obj_path + ", options = " + str(options))
    session.data_objects.put(source_file_path, dest_data_obj_path, **options)
    logger.info("succeeded", task="irods_upload_file", path = source_file_path)


def no_op(hdlr_mod, logger, session, meta, **options):
    pass


def sync_file(hdlr_mod, logger, session, meta, **options):
    target = meta["target"]
    path = meta["path"]
    logger.info("ZZZZ - path:" + path)
    logger.info("syncing object " + target + ", options = " + str(options))

    resc_name = get_resource_name(hdlr_mod, session, meta, **options)

    if resc_name is not None:
        options["destRescName"] = resc_name

    op = hdlr_mod.operation(session, meta, **options)

    if op == Operation.PUT_APPEND:
        BUFFER_SIZE = 1024
        logger.info("appending object " + target + ", options = " + str(options))
        tsize = size(session, target)
        tfd = session.data_objects.open(target, "a", **options)
        tfd.seek(tsize)
        with open(path, "rb") as sfd:
            sfd.seek(tsize)
            while True:
                buf = sfd.read(BUFFER_SIZE)
                if buf == b"":
                    break
                tfd.write(buf)
        tfd.close()
        logger.info("succeeded", task="irods_append_file", path=path)

    else:
        logger.info("uploading object " + target + ", options = " + str(options))
        session.data_objects.put(path, target, **options)
        logger.info("succeeded", task="irods_update_file", path = path)


def update_metadata(hdlr_mod, logger, session, meta, **options):
    target = meta["target"]
    path = meta["path"]
    target_path = get_target_path(hdlr_mod, session, meta, **options)
    if target_path is None:
        target_path = path

    if 'b64_path_str' in meta:
        b64str = meta['b64_path_str']
        b64decoded_utf8_bstr = base64.b64decode(b64str)
        path = b64decoded_utf8_bstr

    size = getsize(path)
    mtime = int(getmtime(path))
    logger.info("updating object: " + target + ", options = " + str(options))

    data_obj_info = {"objPath": target}

    resc_name = get_resource_name(hdlr_mod, session, meta, **options)
    outdated_repl_nums = []
    found = False
    if resc_name is None:
        found = True
    else:
        for row in session.query(Resource.name, DataObject.path, DataObject.replica_number).filter(DataObject.name == basename(target), Collection.name == dirname(target)):
            if row[DataObject.path] == target_path:
                if child_of(session, row[Resource.name], resc_name):
                    found = True
                    repl_num = row[DataObject.replica_number]
                    data_obj_info["replNum"] = repl_num
                    continue

    if not found:
        logger.error("updating object: wrong resource or path, target = " + target + ", path = " + path + ", target_path = " + target_path + ", options = " + str(options))
        raise Exception("wrong resource or path")

    session.data_objects.modDataObjMeta(data_obj_info, {"dataSize":size, "dataModify":mtime, "allReplStatus":1}, **options)

    logger.info("succeeded", task="irods_update_metadata", path = path)


def sync_file_meta(hdlr_mod, logger, session, meta, **options):
    pass


def sync_dir_meta(hdlr_mod, logger, session, meta, **options):
    pass


irods_session_map = {}


def irods_session(hdlr_mod, meta, **options):
    env_irods_host = os.environ.get("IRODS_HOST")
    env_irods_port = os.environ.get("IRODS_PORT")
    env_irods_user_name = os.environ.get("IRODS_USER_NAME")
    env_irods_zone_name = os.environ.get("IRODS_ZONE_NAME")
    env_irods_password = os.environ.get("IRODS_PASSWORD")

    env_file = os.environ.get('IRODS_ENVIRONMENT_FILE')

    kwargs = {}
    if env_irods_host is None or \
            env_irods_port is None or \
            env_irods_user_name is None or \
            env_irods_zone_name is None or \
            env_irods_password is None:
        if env_file is None:
            env_file = os.path.expanduser('~/.irods/irods_environment.json')

        kwargs["irods_env_file"] = env_file
    else:
        kwargs["host"] = env_irods_host
        kwargs["port"] = env_irods_port
        kwargs["user"] = env_irods_user_name
        kwargs["zone"] = env_irods_zone_name
        kwargs["password"] = env_irods_password

    if hasattr(hdlr_mod, "as_user"):
        client_zone, client_user = hdlr_mod.as_user(meta, **options)
        kwargs["client_user"] = client_user
        kwargs["client_zone"] = client_zone

    key = json.dumps(kwargs) # todo add timestamp of env file to key

    sess = irods_session_map.get(key)

    if sess is None:
        # TODO: #42 - pull out 10 into configuration
        for i in range(10):
            try:
                sess = iRODSSession(**kwargs)
                irods_session_map[key] = sess
                break
            except NetworkException:
                if i < 10:
                    time.sleep(0.1)
                else:
                    raise

    return sess


def sync_data_from_file(meta, logger, content, **options):
    target = meta["target"]
    path = meta["path"]
    hdlr_mod = get_hdlr_mod(meta)
    init = meta["initial_ingest"]

    session = irods_session(hdlr_mod, meta, **options)

    if init:
        exists = False
    else:
        if session.data_objects.exists(target):
            exists = True
        elif session.collections.exists(target):
            raise Exception("sync: cannot syncing file " + path + " to collection " + target)
        else:
            exists = False

    if hasattr(hdlr_mod, "operation"):
        op = hdlr_mod.operation(session, meta, **options)
    else:
        op = Operation.REGISTER_SYNC

    if op == Operation.NO_OP:
        if not exists:
            call(hdlr_mod, "on_data_obj_create", no_op, logger, hdlr_mod, logger, session, meta, **options)
        else:
            call(hdlr_mod, "on_data_obj_modify", no_op, logger, hdlr_mod, logger, session, meta, **options)
    else:
        createRepl = False
        if exists and op == Operation.REGISTER_AS_REPLICA_SYNC:
            if hasattr(hdlr_mod, "to_resource"):
                resc_name = hdlr_mod.to_resource(session, meta, **options)
            else:
                raise Exception("no resource name defined")

            found = False
            foundPath = False
            for replica in session.data_objects.get(target).replicas:
                if child_of(session, replica.resource_name, resc_name):
                    found = True
                    if replica.path == path:
                        foundPath = True
            if found:
                if not foundPath:
                    raise Exception("there is at least one replica under resource but all replicas have wrong paths")
            else:
                createRepl = True

        put = op in [Operation.PUT, Operation.PUT_SYNC, Operation.PUT_APPEND]
        sync = op in [Operation.PUT_SYNC, Operation.PUT_APPEND]

        if not exists:
            meta2 = meta.copy()
            meta2["target"] = dirname(target)
            if 'b64_path_str' not in meta2:
                meta2["path"] = dirname(path)
            create_dirs(hdlr_mod, logger, session, meta2, **options)

            if put:
                call(hdlr_mod, "on_data_obj_create", upload_file, logger, hdlr_mod, logger, session, meta, **options)
            else:
                call(hdlr_mod, "on_data_obj_create", register_file, logger, hdlr_mod, logger, session, meta, **options)
        elif createRepl:
            options["regRepl"] = ""

            call(hdlr_mod, "on_data_obj_create", register_file, logger, hdlr_mod, logger, session, meta, **options)
        elif content:
            if put:
                if sync:
                    call(hdlr_mod, "on_data_obj_modify", sync_file, logger, hdlr_mod, logger, session, meta, **options)
            else:
                call(hdlr_mod, "on_data_obj_modify", update_metadata, logger, hdlr_mod, logger, session, meta, **options)
        else:
            call(hdlr_mod, "on_data_obj_modify", sync_file_meta, logger, hdlr_mod, logger, session, meta, **options)

def sync_metadata_from_file(meta, logger, **options):
    sync_data_from_file(meta, logger, False, **options)


def sync_data_from_dir(meta, logger, content, **options):
    target = meta["target"]
    path = meta["path"]
    hdlr_mod = get_hdlr_mod(meta)

    session = irods_session(hdlr_mod, meta, **options)

    exists = session.collections.exists(target)

    if hasattr(hdlr_mod, "operation"):
        op = hdlr_mod.operation(session, meta, **options)
    else:
        op = Operation.REGISTER_SYNC

    if op == Operation.NO_OP:
        if not exists:
            call(hdlr_mod, "on_collection_create", no_op, logger, hdlr_mod, logger, session, meta, **options)
        else:
            call(hdlr_mod, "on_collection_modify", no_op, logger, hdlr_mod, logger, session, meta, **options)
    else:
        if not exists:
            create_dirs(hdlr_mod, logger, session, meta, **options)
        else:
            call(hdlr_mod, "on_collection_modify", sync_dir_meta, logger, hdlr_mod, logger, session, meta, **options)


def sync_metadata_from_dir(meta, logger, **options):
    sync_data_from_dir(meta, logger, False, **options)

def register_link(hdlr_mod, logger, session, meta, **options):
    target = meta["target"]
    path = meta["path"]
    target_path = get_target_path(hdlr_mod, session, meta, **options)
    if target_path is None:
        target_path = path

    resc_name = get_resource_name(hdlr_mod, session, meta, **options)

    if resc_name is not None:
        options["destRescName"] = resc_name

    logger.info("registering link " + target + ", options = " + str(options))

    mtime = int(meta['mtime'])

    data_obj_info = {"objPath": target}
    if resc_name is not None:
        del options["destRescName"]
        for row in session.query(DataObject.replica_number).filter(DataObject.name == basename(target), Collection.name == dirname(target), DataObject.resource_name == resc_name):
            data_obj_info["replNum"] = int(row[DataObject.replica_number])

    logger.info('PROCESSING: in register_link', task='register_link', path=path)
    obj = session.data_objects.create(target, resc_name, **options)

    if meta['is_socket']:
        obj.metadata.add('socket_target', 'socket', 'automated_ingest')
    elif meta['is_link']:
        tgt = os.path.abspath(os.readlink(meta['path']))
        obj.metadata.add('link_target', tgt, 'automated_ingest')

    session.data_objects.modDataObjMeta(data_obj_info, {"dataModify":mtime, "filePath":path}, **options)

    logger.info("succeeded", task="register_link", path = path)

def update_link_metadata(hdlr_mod, logger, session, meta, **options):
    target = meta["target"]
    path = meta["path"]
    target_path = get_target_path(hdlr_mod, session, meta, **options)
    if target_path is None:
        target_path = path

    mtime = int(meta['mtime'])
    logger.info("updating link: " + target + ", options = " + str(options))

    data_obj_info = {"objPath": target}

    resc_name = get_resource_name(hdlr_mod, session, meta, **options)
    outdated_repl_nums = []
    found = False

    if resc_name is None:
        found = True
    else:
        for row in session.query(Resource.name, DataObject.path, DataObject.replica_number).filter(DataObject.name == basename(target), Collection.name == dirname(target)):
            if row[DataObject.path] == path:
                if child_of(session, row[Resource.name], resc_name):
                    found = True
                    repl_num = row[DataObject.replica_number]
                    data_obj_info["replNum"] = repl_num
                    continue

    if not found:
        logger.error("updating object: wrong resource or path, target = " + target + ", path = " + path + ", target_path = " + target_path + ", options = " + str(options))
        raise Exception("wrong resource or path")

    session.data_objects.modDataObjMeta(data_obj_info, {"dataModify":mtime, "allReplStatus":1}, **options)

    logger.info("succeeded", task="irods_update_link_metadata", path = path)


def sync_data_from_link(meta, logger, content, **options):
    target = meta["target"]
    path = meta["path"]
    hdlr_mod = get_hdlr_mod(meta)
    init = meta["initial_ingest"]

    session = irods_session(hdlr_mod, meta, **options)

    if init:
        exists = False
    else:
        if session.data_objects.exists(target):
            exists = True
        elif session.collections.exists(target):
            raise Exception("sync: cannot syncing link " + path + " to collection " + target)
        else:
            exists = False

    if hasattr(hdlr_mod, "operation"):
        op = hdlr_mod.operation(session, meta, **options)
    else:
        op = Operation.REGISTER_SYNC

    if op == Operation.NO_OP:
        if not exists:
            call(hdlr_mod, "on_data_obj_create", no_op, logger, hdlr_mod, logger, session, meta, **options)
        else:
            call(hdlr_mod, "on_data_obj_modify", no_op, logger, hdlr_mod, logger, session, meta, **options)
    else:
        if not exists:
            meta2 = meta.copy()
            meta2["target"] = dirname(target)
            meta2["path"] = dirname(path)
            create_dirs(hdlr_mod, logger, session, meta2, **options)

            call(hdlr_mod, "on_data_obj_create", register_link, logger, hdlr_mod, logger, session, meta, **options)
        else:
            call(hdlr_mod, "on_data_obj_modify", update_link_metadata, logger, hdlr_mod, logger, session, meta, **options)


def sync_metadata_from_link(meta, logger, **options):
    sync_data_from_link(meta, logger, False, **options)




