import os
import re
import json
import uuid
import requests
import shopify
from urllib.parse import urlparse
from typing import List

from config import (
    SHOPIFY_CLIENT_ID,
    SHOPIFY_CLIENT_SECRET,
    SHOPIFY_SHOP_URL,
    SHOPIFY_API_VERSION,
)
from services.gcs import bucket, generate_signed_url

def get_shop_domain(shop_url: str) -> str:
    """Normalizes SHOPIFY_SHOP_URL into a bare shop domain."""
    parsed = urlparse(shop_url)
    if parsed.netloc:
        return parsed.netloc
    cleaned = shop_url.replace("https://", "").replace("http://", "")
    return cleaned.split("/")[0]

def fetch_shopify_access_token() -> str:
    """Fetches a short-lived Shopify Admin access token via client credentials."""
    if not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
        raise ValueError("SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET must be set.")

    shop_domain = get_shop_domain(SHOPIFY_SHOP_URL)
    token_url = f"https://{shop_domain}/admin/oauth/access_token"
    response = requests.post(
        token_url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
        },
        timeout=20,
    )
    response.raise_for_status()
    token_payload = response.json()
    access_token = token_payload.get("access_token")
    if not access_token:
        raise ValueError(f"No access_token in Shopify response: {token_payload}")
    return access_token

def activate_shopify_session_with_fresh_token() -> None:
    """Fetches a fresh access token and activates Shopify session."""
    access_token = fetch_shopify_access_token()
    session = shopify.Session(get_shop_domain(SHOPIFY_SHOP_URL), SHOPIFY_API_VERSION, access_token)
    shopify.ShopifyResource.activate_session(session)

def upload_videos_to_shopify(product_id: int, video_paths: List[str]) -> None:
    """Uploads GCS-hosted videos as Shopify product media via GraphQL."""
    if not video_paths:
        return

    access_token = fetch_shopify_access_token()
    shop_domain = get_shop_domain(SHOPIFY_SHOP_URL)
    graphql_url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    product_gid = f"gid://shopify/Product/{product_id}"

    media_inputs = []
    
    staged_upload_mutation = """
    mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
      stagedUploadsCreate(input: $input) {
        stagedTargets {
          url
          resourceUrl
          parameters {
            name
            value
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """

    for path in video_paths:
        filename = os.path.basename(path)
        mime_type = "video/quicktime" if filename.lower().endswith(".mov") else "video/mp4"
        
        print(f"Downloading video from GCS for staging: {path}")
        blob = bucket.blob(path)
        video_bytes = blob.download_as_bytes()
        file_size_str = str(len(video_bytes))
        
        # 1. Request staging URL
        stage_response = requests.post(
            graphql_url,
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": access_token,
            },
            json={
                "query": staged_upload_mutation,
                "variables": {
                    "input": [{
                        "resource": "VIDEO",
                        "filename": filename,
                        "mimeType": mime_type,
                        "fileSize": file_size_str,
                        "httpMethod": "POST"
                    }]
                }
            },
            timeout=30,
        )
        stage_response.raise_for_status()
        stage_payload = stage_response.json()
        
        if stage_payload.get("errors"):
            raise ValueError(f"GraphQL errors requesting staged upload: {stage_payload['errors']}")
            
        stage_data = stage_payload.get("data", {}).get("stagedUploadsCreate", {})
        if stage_data.get("userErrors"):
            raise ValueError(f"Staged upload user errors: {stage_data['userErrors']}")
            
        target = stage_data["stagedTargets"][0]
        upload_url = target["url"]
        resource_url = target["resourceUrl"]
        parameters = {p["name"]: p["value"] for p in target["parameters"]}
        
        print(f"Uploading video to Shopify staging ({len(video_bytes)} bytes)...")
        # 2. Upload file to staging URL
        files = {"file": (filename, video_bytes, mime_type)}
        upload_response = requests.post(upload_url, data=parameters, files=files, timeout=120)
        upload_response.raise_for_status()
        
        print(f"✅ Staged video successfully: {resource_url}")
        
        # 3. Add to media inputs for productCreateMedia
        media_inputs.append(
            {
                "mediaContentType": "VIDEO",
                "originalSource": resource_url,
            }
        )

    mutation = """
    mutation productCreateMedia($media: [CreateMediaInput!]!, $productId: ID!) {
      productCreateMedia(media: $media, productId: $productId) {
        media {
          alt
          mediaContentType
          status
        }
        mediaUserErrors {
          field
          message
        }
        product {
          id
        }
      }
    }
    """

    response = requests.post(
        graphql_url,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token,
        },
        json={
            "query": mutation,
            "variables": {
                "media": media_inputs,
                "productId": product_gid,
            },
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()

    errors = payload.get("errors")
    if errors:
        print(f"Shopify GraphQL top-level errors: {json.dumps(errors, ensure_ascii=True)}")
        print(f"Shopify raw productCreateMedia payload: {json.dumps(payload, ensure_ascii=True)}")
        raise ValueError(f"Shopify GraphQL errors: {errors}")

    media_result = payload.get("data", {}).get("productCreateMedia", {})
    media_user_errors = media_result.get("mediaUserErrors", [])
    if media_user_errors:
        print(f"Shopify mediaUserErrors: {json.dumps(media_user_errors, ensure_ascii=True)}")
        print(
            "Shopify productCreateMedia debug context: "
            + json.dumps(
                {
                    "product_gid": product_gid,
                    "video_paths": video_paths,
                    "media_inputs": media_inputs,
                    "media_response": media_result.get("media", []),
                },
                ensure_ascii=True,
            )
        )
        raise ValueError(f"Shopify media user errors: {media_user_errors}")

def publish_product_to_all_channels(product_id: int) -> int:
    """Publishes a product to all available Shopify sales channels."""
    access_token = fetch_shopify_access_token()
    shop_domain = get_shop_domain(SHOPIFY_SHOP_URL)
    graphql_url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    product_gid = f"gid://shopify/Product/{product_id}"

    publications_query = """
    query GetPublications {
      publications(first: 250) {
        nodes {
          id
          name
        }
      }
    }
    """
    query_response = requests.post(
        graphql_url,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token,
        },
        json={"query": publications_query},
        timeout=30,
    )
    query_response.raise_for_status()
    query_payload = query_response.json()
    if query_payload.get("errors"):
        raise ValueError(f"Shopify publication query errors: {query_payload['errors']}")

    publications = query_payload.get("data", {}).get("publications", {}).get("nodes", [])
    publication_ids = [publication.get("id") for publication in publications if publication.get("id")]
    if not publication_ids:
        print("No Shopify publications found; skipping channel publishing.")
        return 0

    publish_mutation = """
    mutation PublishToChannels($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        publishable {
          availablePublicationsCount {
            count
          }
          resourcePublicationsCount {
            count
          }
        }
        shop {
          id
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    publish_input = [{"publicationId": publication_id} for publication_id in publication_ids]
    publish_response = requests.post(
        graphql_url,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token,
        },
        json={
            "query": publish_mutation,
            "variables": {
                "id": product_gid,
                "input": publish_input,
            },
        },
        timeout=30,
    )
    publish_response.raise_for_status()
    publish_payload = publish_response.json()
    if publish_payload.get("errors"):
        raise ValueError(f"Shopify publish mutation errors: {publish_payload['errors']}")

    user_errors = (
        publish_payload.get("data", {})
        .get("publishablePublish", {})
        .get("userErrors", [])
    )
    if user_errors:
        raise ValueError(f"Shopify publish user errors: {user_errors}")

    return len(publication_ids)

def set_inventory_quantity(inventory_item_id: int, quantity: int = 1) -> None:
    """Sets the available inventory quantity using the inventorySetQuantities GraphQL mutation."""
    access_token = fetch_shopify_access_token()
    shop_domain = get_shop_domain(SHOPIFY_SHOP_URL)
    graphql_url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    
    # Fetch first active location using python shopify API
    locations = shopify.Location.find()
    if not locations:
        print("No locations found to set inventory.")
        return
        
    location_gid = f"gid://shopify/Location/{locations[0].id}"
    inventory_item_gid = f"gid://shopify/InventoryItem/{inventory_item_id}"
    
    import uuid
    idempotency_key = str(uuid.uuid4())
    
    mutation = f"""
    mutation inventorySetQuantities($input: InventorySetQuantitiesInput!) {{
      inventorySetQuantities(input: $input) @idempotent(key: "{idempotency_key}") {{
        userErrors {{
          field
          message
        }}
      }}
    }}
    """
    
    variables = {
        "input": {
            "name": "available",
            "reason": "correction",
            "quantities": [
                {
                    "inventoryItemId": inventory_item_gid,
                    "locationId": location_gid,
                    "quantity": quantity,
                    "changeFromQuantity": 0
                }
            ]
        }
    }
    
    response = requests.post(
        graphql_url,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token,
        },
        json={"query": mutation, "variables": variables},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    
    if payload.get("errors"):
        raise ValueError(f"GraphQL errors setting inventory: {payload['errors']}")
        
    user_errors = payload.get("data", {}).get("inventorySetQuantities", {}).get("userErrors", [])
    if user_errors:
        raise ValueError(f"Inventory user errors: {user_errors}")

def update_product_category(product_id: int, category_string: str) -> None:
    """Sets the Shopify product category (taxonomy) using the modern 2026-04 GraphQL API."""
    if not category_string:
        return

    access_token = fetch_shopify_access_token()
    shop_domain = get_shop_domain(SHOPIFY_SHOP_URL)
    graphql_url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    product_gid = f"gid://shopify/Product/{product_id}"

    # 1. Fetch product details to use as search context
    product_query = """
    query GetProductDetails($id: ID!) {
      product(id: $id) {
        title
      }
    }
    """
    product_response = requests.post(
        graphql_url,
        headers={"Content-Type": "application/json", "X-Shopify-Access-Token": access_token},
        json={"query": product_query, "variables": {"id": product_gid}},
        timeout=30,
    )
    product_response.raise_for_status()
    title = product_response.json().get("data", {}).get("product", {}).get("title", "")

    # 2. Resolve the category using the modern 'taxonomy' query
    # We try the full breadcrumb, then the leaf category, then the product title.
    taxonomy_query = """
    query ResolveTaxonomy($search: String!) {
      taxonomy {
        categories(first: 1, search: $search) {
          nodes {
            id
            fullName
          }
        }
      }
    }
    """

    def search_taxonomy(term):
        if not term: return None
        resp = requests.post(
            graphql_url,
            headers={"Content-Type": "application/json", "X-Shopify-Access-Token": access_token},
            json={"query": taxonomy_query, "variables": {"search": term}},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            print(f"⚠️ Taxonomy query error for '{term}': {payload['errors']}")
            return None
        nodes = payload.get("data", {}).get("taxonomy", {}).get("categories", {}).get("nodes", [])
        return nodes[0] if nodes else None

    # Try 1: Full AI-generated breadcrumb
    target_node = search_taxonomy(category_string)
    
    # Try 2: Leaf category name
    if not target_node and " > " in category_string:
        leaf = category_string.split(" > ")[-1].strip()
        target_node = search_taxonomy(leaf)
        
    # Try 3: Product title
    if not target_node:
        target_node = search_taxonomy(title)

    if not target_node:
        print(f"⚠️ Failed to resolve Shopify taxonomy category for product {product_id} after all attempts.")
        return

    target_category_gid = target_node["id"]
    print(f"✅ Resolved category: {target_node['fullName']} ({target_category_gid})")

    # 3. Update the product with the new 'category' field (standardized in 2026-04)
    update_mutation = """
    mutation productUpdate($input: ProductInput!) {
      productUpdate(input: $input) {
        product {
          id
          category {
            fullName
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    
    update_variables = {
        "input": {
            "id": product_gid,
            "category": target_category_gid
        }
    }
    
    update_response = requests.post(
        graphql_url,
        headers={"Content-Type": "application/json", "X-Shopify-Access-Token": access_token},
        json={"query": update_mutation, "variables": update_variables},
        timeout=30,
    )
    update_response.raise_for_status()
    update_payload = update_response.json()
    
    user_errors = update_payload.get("data", {}).get("productUpdate", {}).get("userErrors", [])
    if user_errors:
        print(f"⚠️ Shopify category update failed: {user_errors}")
    else:
        print(f"✅ Successfully set Shopify product category for product {product_id}")

def resolve_metaobject_gid(type_name: str, search_value: str) -> str:
    """
    Resolves a Shopify Metaobject GID by searching for its display name or handle.
    Example: resolve_metaobject_gid('shopify--target-gender', 'Female')
    """
    if not search_value:
        return None

    access_token = fetch_shopify_access_token()
    shop_domain = get_shop_domain(SHOPIFY_SHOP_URL)
    graphql_url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"

    # We query for all metaobjects of this type and do a case-insensitive match.
    # Standard taxonomy metaobjects are usually few enough to fetch in one go (or first 100).
    query = """
    query GetMetaobjects($type: String!) {
      metaobjects(type: $type, first: 100) {
        nodes {
          id
          handle
          displayName
        }
      }
    }
    """
    
    resp = requests.post(
        graphql_url,
        headers={"Content-Type": "application/json", "X-Shopify-Access-Token": access_token},
        json={"query": query, "variables": {"type": type_name}},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    
    if payload.get("errors"):
        print(f"⚠️ Error resolving metaobject for {type_name}: {payload['errors']}")
        return None
        
    nodes = payload.get("data", {}).get("metaobjects", {}).get("nodes", [])
    if not nodes:
        print(f"⚠️ No metaobjects found for type {type_name}. Ensure they are 'activated' in Shopify Admin.")
        return None

    search_lower = search_value.lower()
    
    # Normalization for common search terms
    normalized_searches = [search_lower]
    if type_name == "shopify--size":
        # Handle X -> XL mapping (e.g. 3X -> 3XL)
        x_match = re.match(r'^([1-9])x$', search_lower)
        if x_match:
            digit = x_match.group(1)
            normalized_searches.append(f"{digit}xl")
        
        # Specific common overrides
        overrides = {
            "1x": ["xl", "1xl"],
            "2x": ["2xl"],
            "3x": ["3xl"],
            "4x": ["4xl"],
            "5x": ["5xl"],
            "xl": ["1x", "1xl"],
            "small": ["s"],
            "medium": ["m"],
            "large": ["l"],
            "extra large": ["xl", "1xl"],
        }
        if search_lower in overrides:
            for val in overrides[search_lower]:
                if val not in normalized_searches:
                    normalized_searches.append(val)
    
    for s in normalized_searches:
        # Try exact match on displayName first
        for node in nodes:
            if node.get("displayName", "").lower() == s:
                return node["id"]
                
        # Try match on handle
        for node in nodes:
            if node.get("handle", "").lower() == s:
                return node["id"]
                
        # Try partial match on displayName
        for node in nodes:
            if s in node.get("displayName", "").lower():
                return node["id"]

    # If we got here, we failed to find a match. Log what WAS available.
    available_names = [n.get("displayName") for n in nodes if n.get("displayName")]
    print(f"⚠️ Could not resolve '{search_value}' (tried {normalized_searches}) for {type_name}.")
    print(f"ℹ️ Available {type_name} in store: {', '.join(available_names[:20])}")
    return None

def set_category_metafields(product_id: int, gender: str, size: str) -> None:
    """Sets the Shopify category metafields (e.g. Target Gender, Size) using the GraphQL API."""
    if not gender and not size:
        return

    access_token = fetch_shopify_access_token()
    shop_domain = get_shop_domain(SHOPIFY_SHOP_URL)
    graphql_url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    product_gid = f"gid://shopify/Product/{product_id}"

    metafields = []
    
    # 1. Resolve Gender Metaobject
    if gender:
        gender_gid = resolve_metaobject_gid("shopify--target-gender", gender)
        if gender_gid:
            # Standard Category Metafields like Target Gender are often list.metaobject_reference
            # Note: The key is target-gender (hyphen), not target_gender (underscore)
            metafields.append({
                "ownerId": product_gid,
                "namespace": "shopify",
                "key": "target-gender",
                "value": json.dumps([gender_gid]),
                "type": "list.metaobject_reference"
            })
            print(f"✅ Resolved Gender '{gender}' to {gender_gid}")
        else:
            print(f"⚠️ Could not resolve GID for Gender: {gender}")

    # 2. Resolve Size Metaobject
    if size:
        size_gid = resolve_metaobject_gid("shopify--size", size)
        if size_gid:
            metafields.append({
                "ownerId": product_gid,
                "namespace": "shopify",
                "key": "size",
                "value": json.dumps([size_gid]),
                "type": "list.metaobject_reference"
            })
            print(f"✅ Resolved Size '{size}' to {size_gid}")
        else:
            print(f"⚠️ Could not resolve GID for Size: {size}")

    if not metafields:
        return

    # Using productUpdate instead of metafieldsSet for potentially better access to shopify namespace
    mutation = """
    mutation productUpdate($input: ProductInput!) {
      productUpdate(input: $input) {
        product {
          id
          metafields(first: 10) {
            nodes {
              key
              value
            }
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    
    # We need to format metafields for ProductInput which uses 'metafields' field
    # ProductInput metafields don't need 'ownerId' inside each metafield object
    product_metafields = []
    for m in metafields:
        product_metafields.append({
            "namespace": m["namespace"],
            "key": m["key"],
            "value": m["value"],
            "type": m["type"]
        })

    variables = {
        "input": {
            "id": product_gid,
            "metafields": product_metafields
        }
    }

    resp = requests.post(
        graphql_url,
        headers={"Content-Type": "application/json", "X-Shopify-Access-Token": access_token},
        json={"query": mutation, "variables": variables},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    
    user_errors = payload.get("data", {}).get("productUpdate", {}).get("userErrors", [])
    if user_errors:
        print(f"⚠️ Category metafields set failed: {user_errors}")
    else:
        print(f"✅ Successfully updated category metafields for product {product_id}")
