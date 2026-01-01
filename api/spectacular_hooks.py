def tag_operations(result, generator, request=None, public=False):
    """Post-processing hook for drf-spectacular to add tags to operations.

    This assigns human-friendly group names based on URL path prefixes
    matching the section comments in `api/urls.py`.
    """
    if not result or 'paths' not in result:
        return result

    # mapping of path prefix -> tag name (order matters; first match wins)
    mapping = [
        (('auth/',), 'Authentication'),
        (('profile/',), 'Profile'),
        (('my-library',), 'Library'),
        (('home/',), 'Home'),
        (('artist/',), 'Artist'),
        (('genres/', 'subgenres/', 'moods/', 'tags/'), 'Classification'),
        (('search/', 'event-playlists', 'search/sections'), 'Search'),
        (('admin/',), 'Admin'),
        # Utility / detail screens / action endpoints (catch-all for common single endpoints)
        (('follow', 'artists/', 'albums/', 'songs/', 'stream/', 'playlists/', 'play/count', 'ads/', 'rules', 'notifications', 'reports'), 'Utility'),
    ]

    for path, path_item in result.get('paths', {}).items():
        # normalize path (strip leading slash)
        normalized = path.lstrip('/')
        assigned = None
        for prefixes, tag in mapping:
            for p in prefixes:
                if normalized.startswith(p):
                    assigned = tag
                    break
            if assigned:
                break

        if not assigned:
            assigned = 'Other'

        # apply tag to all operations under this path
        for method_name, operation in list(path_item.items()):
            if method_name.startswith('x-'):
                continue
            if isinstance(operation, dict):
                # respect existing tags if present (prepend our tag)
                existing = operation.get('tags') or []
                if assigned not in existing:
                    operation['tags'] = [assigned] + existing

    return result
