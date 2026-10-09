package com.example.catalog.cart;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

/** A row of {@code products} (schema.sql). Money is integer cents. */
@JsonIgnoreProperties(ignoreUnknown = true)
public class Product {
    public String id;
    public String sku;
    public String name;
    public String description;
    public long price_cents;
    public String currency;
    public String image_url;
    public int stock;
    public boolean active;
}
